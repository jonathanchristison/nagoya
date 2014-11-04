#!/usr/bin/env python2

# References:
# https://fedoraproject.org/wiki/Koji/ServerHowTo
# https://github.com/sbadakhc/kojak/blob/master/scripts/install/install

import util.cfg as cfg
import util.pkg as pkg
import util.cred as cred
from util.log import log

#
# Setup
#

log.info("General update")
pkg.clean()
pkg.update()

log.info("Install EPEL")
pkg.install("https://dl.fedoraproject.org/pub/epel/6/x86_64/epel-release-6-8.noarch.rpm")

#
# Kojid (Koji Builder)
#

log.info("Install Koji Builder")
pkg.install("koji-builder")

koji_url = dict()
koji_url["web"] = "http://koji/koji"
koji_url["top"] = "http://koji/kojifiles"
koji_url["hub"] = "http://koji/kojihub"

log.info("Configure Koji Builder")
with cfg.mod_ini("/etc/kojid/kojid.conf") as i:
    i.kojid.sleeptime = 2
    i.kojid.maxjobs = 20
    i.kojid.server = koji_url["hub"]
    i.kojid.topurl = koji_url["top"]
#   i.kojid.cert is set at runtime
    i.kojid.ca = cred.ca_crt
    i.kojid.serverca = cred.ca_crt
    i.kojid.smtphost = "koji"
    i.kojid.from_addr = "Koji Build System <buildsys@kojibuilder>"

log.info("Modify mock to not call unshare")
with cfg.mod_text("/usr/lib/python2.6/site-packages/mockbuild/util.py") as lines:
    i = lines.index("def unshare(flags):\n")
    lines.insert(i+1, "    return\n")

log.info("Modify mock to use Docker's system vfs mounts")
with cfg.mod_text("/usr/lib/python2.6/site-packages/mockbuild/mounts.py") as lines:
    dict_ins_index = lines.index("class FileSystemMountPoint(MountPoint):\n")
    lines.insert(dict_ins_index-1, """
type_paths = {
    "proc" : "/proc"
    "sysfs" : "/sys"
    "tmpfs" : "/dev/shm"
    "devpts" : "/dev/pts"
}
""")
    mount_ins_index = lines.index("    def mount(self):\n")
    lines.insert(dict_ins_index+3, """
        if self.filetype in type_paths:
            link_from = type_paths[self.filetype]
            mockbuild.util.rmtree(self.path)
            os.symlink(link_from, self.path)
        self.mounted = True
        return True
""")
    mount_ins_index = lines.index("    def umount(self, force=False, nowarn=False):\n")
    lines.insert(dict_ins_index+3, """
        if self.filetype in type_paths:
            os.remove(self.path)
        self.mounted = False
        return True
""")

#
# Koji CLI
#

log.info("Configure Koji CLI")
with cfg.mod_ini("/etc/koji.conf") as i:
    i.koji.server = koji_url["hub"]
    i.koji.weburl = koji_url["web"]
    i.koji.topurl = koji_url["top"]
    i.koji.topdir = "/mnt/koji"
    i.koji.cert = cred.user["kojiadmin"].pem
    i.koji.ca = cred.ca_crt
    i.koji.serverca = cred.ca_crt

pkg.clean()
