# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
Tests for ``flocker.node.agents.blockdevice_manager``.
"""

from uuid import uuid4

from testtools import ExpectedException
from testtools.matchers import Not, FileExists

from zope.interface.verify import verifyObject

from ....testtools import TestCase, random_name

from ..blockdevice_manager import (
    BindMountError,
    BlockDeviceManager,
    IBlockDeviceManager,
    Losetup,
    MakeFilesystemError,
    MakeTmpfsMountError,
    MountError,
    MountInfo,
    Permissions,
    RemountError,
    temporary_mount,
    UnmountError,
)
from ..loopback import LOOPBACK_MINIMUM_ALLOCATABLE_SIZE
from ..testtools import (
    loopbackblockdeviceapi_for_test,
    mountroot_for_test,
)


class BlockDeviceManagerTests(TestCase):
    """
    Tests for flocker.node.agents.blockdevice_manager.BlockDeviceManager.
    """

    def setUp(self):
        """
        Establish testing infrastructure for test cases.
        """
        super(BlockDeviceManagerTests, self).setUp()
        self.loopback_api = loopbackblockdeviceapi_for_test(self)
        self.manager_under_test = BlockDeviceManager()
        self.mountroot = mountroot_for_test(self)

    def _get_directory_for_mount(self):
        """
        Construct a temporary directory to be used as a mountpoint.
        """
        directory = self.mountroot.child(str(uuid4()))
        directory.makedirs()
        return directory

    def _get_free_blockdevice(self):
        """
        Construct a new blockdevice for testing purposes.
        """
        volume = self.loopback_api.create_volume(
            dataset_id=uuid4(), size=LOOPBACK_MINIMUM_ALLOCATABLE_SIZE)
        self.loopback_api.attach_volume(
            volume.blockdevice_id, self.loopback_api.compute_instance_id())
        return self.loopback_api.get_device_path(volume.blockdevice_id)

    def test_implements_interface(self):
        """
        ``BlockDeviceManager`` implements ``IBlockDeviceManager``.
        """
        self.assertTrue(verifyObject(IBlockDeviceManager,
                                     self.manager_under_test))

    def test_get_mounts_shows_only_mounted(self):
        """
        Only mounted blockdevices appear in get_mounts.
        """
        blockdevice = self._get_free_blockdevice()
        mountpoint = self._get_directory_for_mount()
        self.manager_under_test.make_filesystem(blockdevice, 'ext4')
        self.manager_under_test.mount(blockdevice, mountpoint)
        mount_info = MountInfo(blockdevice=blockdevice, mountpoint=mountpoint)
        self.assertIn(mount_info, self.manager_under_test.get_mounts())
        self.manager_under_test.unmount(blockdevice)
        self.assertNotIn(mount_info, self.manager_under_test.get_mounts())

    def test_mount_multiple_times(self):
        """
        Mounting a device to n different locations requires n unmounts.

        Also verify they are unmounted in LIFO order.
        """
        blockdevice = self._get_free_blockdevice()
        self.manager_under_test.make_filesystem(blockdevice, 'ext4')
        mountpoints = list(self._get_directory_for_mount() for _ in xrange(4))
        for mountpoint in mountpoints:
            self.manager_under_test.mount(blockdevice, mountpoint)

        mount_infos = list(MountInfo(blockdevice=blockdevice,
                                     mountpoint=mountpoint)
                           for mountpoint in mountpoints)
        while mount_infos:
            self.assertSetEqual(
                set(mount_infos),
                set(m for m in self.manager_under_test.get_mounts()
                    if m.blockdevice == blockdevice))
            self.manager_under_test.unmount(blockdevice)
            mount_infos.pop()
        self.assertFalse(any(m.blockdevice == blockdevice
                             for m in self.manager_under_test.get_mounts()))

    def test_mount_multiple_blockdevices(self):
        """
        Mounting multiple devices to the same mountpoint.

        Note that the blockdevices must be unmounted in reverse order,
        otherwise the unmount operations will fail.
        """
        blockdevices = list(self._get_free_blockdevice() for _ in xrange(4))
        mountpoint = self._get_directory_for_mount()
        for blockdevice in blockdevices:
            self.manager_under_test.make_filesystem(blockdevice, 'ext4')
            self.manager_under_test.mount(blockdevice, mountpoint)

        mount_infos = list(MountInfo(blockdevice=blockdevice,
                                     mountpoint=mountpoint)
                           for blockdevice in blockdevices)

        blockdevices.reverse()
        for blockdevice in blockdevices:
            self.assertSetEqual(
                set(mount_infos),
                set(m for m in self.manager_under_test.get_mounts()
                    if m.mountpoint == mountpoint))
            self.manager_under_test.unmount(blockdevice)
            mount_infos = list(m for m in mount_infos
                               if m.blockdevice != blockdevice)

        self.assertSetEqual(
            set(), set(m for m in self.manager_under_test.get_mounts()
                       if m.mountpoint == mountpoint))

    def test_unmount_unmounted(self):
        """
        Errors in unmounting raise an ``UnmountError``.
        """
        blockdevice = self._get_free_blockdevice()
        with self.assertRaisesRegexp(UnmountError, blockdevice.path):
            self.manager_under_test.unmount(blockdevice)

    def test_mount_unformatted(self):
        """
        Errors in mounting raise a ``MountError``.
        """
        blockdevice = self._get_free_blockdevice()
        mountpoint = self._get_directory_for_mount()
        with self.assertRaisesRegexp(MountError, blockdevice.path):
            self.manager_under_test.mount(blockdevice, mountpoint)

    def test_formatted_bad_type(self):
        """
        Errors in formatting raise a ``MakeFilesystemError``.
        """
        blockdevice = self._get_free_blockdevice()
        with self.assertRaisesRegexp(MakeFilesystemError, blockdevice.path):
            self.manager_under_test.make_filesystem(blockdevice, 'myfakeyfs')

    def test_bind_mount(self):
        """
        Files created in a bind mount are visible in the original folder and
        vice versa.
        """
        src_directory = self._get_directory_for_mount()
        target_directory = self._get_directory_for_mount()
        self.manager_under_test.bind_mount(src_directory, target_directory)
        self.addCleanup(self.manager_under_test.unmount, target_directory)
        for create, view in [(target_directory, src_directory),
                             (src_directory, target_directory)]:
            filename = str(uuid4())
            new_file = create.child(filename)
            new_file.touch()
            self.expectThat(view.child(filename).path, FileExists(),
                            'Created file not visible through bind mount.')

    def test_failing_bind_mount(self):
        """
        Attempts to bind mount to a mountpoint that does not exist fail with a
        ``BindMountError``.
        """
        src_directory = self._get_directory_for_mount()
        target_directory = self._get_directory_for_mount().child('nonexistent')
        with ExpectedException(BindMountError, '.*nonexistent.*'):
            self.manager_under_test.bind_mount(src_directory, target_directory)

    def test_remount(self):
        """
        Mounts remounted read-only cannot be written to until they are
        remounted read-write.
        """
        blockdevice = self._get_free_blockdevice()
        mountpoint = self._get_directory_for_mount()
        self.manager_under_test.make_filesystem(blockdevice, 'ext4')
        self.manager_under_test.mount(blockdevice, mountpoint)

        first_file = mountpoint.child(str(uuid4()))
        second_file = mountpoint.child(str(uuid4()))
        third_file = mountpoint.child(str(uuid4()))

        first_file.touch()
        self.expectThat(first_file.path, FileExists())

        self.manager_under_test.remount(mountpoint, Permissions.READ_ONLY)
        self.expectThat(first_file.path, FileExists())

        with ExpectedException(OSError):
            second_file.touch()
        self.expectThat(second_file.path, Not(FileExists()))

        self.manager_under_test.remount(mountpoint, Permissions.READ_WRITE)
        self.expectThat(first_file.path, FileExists())
        self.expectThat(second_file.path, Not(FileExists()))
        third_file.touch()
        self.expectThat(third_file.path, FileExists())

    def test_remount_failure(self):
        """
        Remounts of a folder that is not mounted fail with ``RemountError``.
        """
        unmounted_directory = self._get_directory_for_mount()
        with ExpectedException(RemountError):
            self.manager_under_test.remount(unmounted_directory,
                                            Permissions.READ_WRITE)

    def test_make_tmpfs_mount(self):
        """
        make_tmpfs_mount should create a tmpfs mountpoint that can be written
        to. Once the mount is unmounted all files should be gone.
        """
        mountpoint = self._get_directory_for_mount()

        test_file = mountpoint.child(unicode(uuid4()))

        self.manager_under_test.make_tmpfs_mount(mountpoint)
        self.expectThat(test_file.path, Not(FileExists()))
        test_file.touch()
        self.expectThat(test_file.path, FileExists(),
                        'File did not exist after being touched on tmpfs.')
        self.manager_under_test.unmount(mountpoint)
        self.expectThat(test_file.path, Not(FileExists()),
                        'File persisted after tmpfs mount unmounted')

    def test_make_tmpfs_mount_failure(self):
        """
        make_tmpfs_mount errors with a ``MakeTmpfsMountError`` if the mount
        point does not exist.
        """
        non_existent = self._get_directory_for_mount().child('non_existent')
        with ExpectedException(MakeTmpfsMountError, '.*non_existent.*'):
            self.manager_under_test.make_tmpfs_mount(non_existent)


class TemporaryMountTests(TestCase):
    def setUp(self):
        super(TemporaryMountTests, self).setUp()
        losetup = Losetup()
        backing_file = self.make_temporary_file()
        with backing_file.open('wb') as f:
            f.truncate(LOOPBACK_MINIMUM_ALLOCATABLE_SIZE)
        self.device_path = losetup.add(
            backing_file=backing_file
        )
        self.addCleanup(lambda: losetup.remove(self.device_path))
        self.bdm = BlockDeviceManager()
        self.bdm.make_filesystem(
            blockdevice=self.device_path,
            filesystem=u"ext4"
        )

    def test_mount(self):
        """
        ``temporary_mount`` mounts the supplied device.
        """
        filename = random_name(self)
        paths = []
        with temporary_mount(
                self.device_path,
        ) as path:
            paths.append(path)
            path.child(filename).setContent(filename)

        with temporary_mount(
                self.device_path,
        ) as path:
            paths.append(path)
            # The randomly named child file can be read from the new mountpoint
            # and has the same content.
            self.assertEqual(filename, path.child(filename).getContent())

        # The mountpoint is different each time.
        self.assertNotEqual(*paths)

        # The mountpoints are deleted by the context manager.
        for path in paths:
            self.assertFalse(
                path.exists(), "Mountpoint still exists: {}".format(path)
            )

        # The device is unmounted by the context manager.
        self.assertNotIn(
            self.device_path,
            [m.blockdevice for m in self.bdm.get_mounts()]
        )

    def test_error(self):
        """
        ``temporary_mount`` unmounts and removes the
        """
        paths = []

        class SomeException(Exception):
            pass

        try:
            with temporary_mount(
                    self.device_path,
            ) as path:
                paths.append(path)
                raise SomeException()
        except SomeException:
            pass
        else:
            self.fail("Exception not raised")
        # The mountpoints are deleted by the context manager.
        for path in paths:
            self.assertFalse(
                path.exists(), "Mountpoint still exists: {}".format(path)
            )

        # The device is unmounted by the context manager.
        self.assertNotIn(
            self.device_path,
            [m.blockdevice for m in self.bdm.get_mounts()]
        )
