import base
from decorators import *
from subprocess import Popen,PIPE
import tuned.logs
import tuned.utils.commands
import glob

log = tuned.logs.get()

class MountsPlugin(base.Plugin):
	"""
	Plugin for tuning options of mount-points.
	"""

	@classmethod
	def _generate_mountpoint_topology(cls):
		"""
		Gets the information about disks, partitions and mountpoints. Stores information about used filesystem and
		creates a list of all underlying devices (in case of LVM) for each mountpoint.
		"""
		mountpoint_topology = {}
		current_disk = None

		stdout, stderr = Popen(["/usr/bin/lsblk", "-rno", "TYPE,RM,KNAME,FSTYPE,MOUNTPOINT"], stdout=PIPE, stderr=PIPE, close_fds=True).communicate()
		for columns in map(lambda line: line.split(), stdout.splitlines()):
			device_type, device_removable, device_name = columns[:3]
			filesystem = columns[3] if len(columns) > 3 else None
			mountpoint = columns[4] if len(columns) > 4 else None

			if device_type == "disk":
				current_disk = device_name
				continue

			# skip removable, skip nonpartitions
			if device_removable == "1" or device_type not in ["part", "lvm"]:
				continue

			if mountpoint is None or mountpoint == "[SWAP]":
				continue

			mountpoint_topology.setdefault(mountpoint, {"disks": set(), "device_name": device_name, "filesystem": filesystem})
			mountpoint_topology[mountpoint]["disks"].add(current_disk)

		cls._mountpoint_topology = mountpoint_topology

	def _init_devices(self):
		self._generate_mountpoint_topology()
		self._devices = set(self._mountpoint_topology.keys())
		self._assigned_devices = set()
		self._free_devices = self._devices.copy()

	def _get_config_options(self):
		return {
			"disable_barriers": None,
		}

	def _instance_init(self, instance):
		instance._has_dynamic_tuning = False
		instance._has_static_tuning = True

	def _instance_cleanup(self, instance):
		pass

	def _get_device_cache_type(self, device):
		"""
		Get device cache type. This will work only for devices on SCSI kernel subsystem.
		"""
		source_filenames = glob.glob("/sys/block/%s/device/scsi_disk/*/cache_type" % device)
		for source_filename in source_filenames:
			return tuned.utils.commands.read_file(source_filename).strip()
		return None

	def _mountpoint_has_writeback_cache(self, mountpoint):
		"""
		Checks if the device has 'write back' cache. If the cache type cannot be determined, asume some other cache.
		"""
		for device in self._mountpoint_topology[mountpoint]["disks"]:
			if self._get_device_cache_type(device) == "write back":
				return True
		return False

	def _mountpoint_has_barriers(self, mountpoint):
		"""
		Checks if a given mountpoint is mounted with barriers enabled or disabled.
		"""
		with open("/proc/mounts") as mounts_file:
			for line in mounts_file:
				# device mountpoint filesystem options dump check
				columns = line.split()
				if columns[0][0] != "/":
					continue
				if columns[1] == mountpoint:
					option_list = columns[3]
					break
			else:
				return None

		options = option_list.split(",")
		for option in options:
			(name, sep, value) = option.partition("=")
			# nobarrier barrier=0
			if name == "nobarrier" or (name == "barrier" and value == "0"):
				return False
			# barrier barrier=1
			elif name == "barrier":
				return True
		else:
			# default
			return True

	def _remount_partition(self, partition, options):
		"""
		Remounts partition.
		"""
		remount_command = ["/usr/bin/mount", partition, "-o", "remount,%s" % options]
		tuned.utils.commands.execute(remount_command)

	@command_custom("disable_barriers", per_device=True)
	def _disable_barriers(self, start, value, mountpoint):
		storage_key = self._storage_key("disable_barriers", mountpoint)
		force = str(value).lower() == "force"
		value = force or self._option_bool(value)

		if start:
			if not value:
				return

			reject_reason = None

			if not self._mountpoint_topology[mountpoint]["filesystem"].startswith("ext"):
				reject_reason = "filesystem not supported"
			elif not force and self._mountpoint_has_writeback_cache(mountpoint):
				reject_reason = "device uses write back cache"
			else:
				original_value = self._mountpoint_has_barriers(mountpoint)
				if original_value is None:
					reject_reason = "unknown current setting"
				elif original_value == False:
					reject_reason = "barriers already disabled"

			if reject_reason is not None:
				log.info("not disabling barriers on '%s' (%s)" % (mountpoint, reject_reason))
				return

			self._storage.set(storage_key, original_value)
			log.info("disabling barriers on '%s'" % mountpoint)
			self._remount_partition(mountpoint, "barrier=0")

		else:
			original_value = self._storage.get(storage_key)
			if original_value is None:
				return

			log.info("enabling barriers on '%s'" % mountpoint)
			self._remount_partition(mountpoint, "barrier=1")
			self._storage.unset(storage_key)
