# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2015, 2016 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import glob
import itertools
import logging
import os
import platform
import re
import string
import shutil
import stat
import subprocess
import urllib
import urllib.request

import snapcraft
from snapcraft.internal import common
from snapcraft.internal import zypper


_BIN_PATHS = (
    'bin',
    'sbin',
    'usr/bin',
    'usr/sbin',
)

logger = logging.getLogger(__name__)

_DEFAULT_SOURCES = (
    ('http://download.opensuse.org/${release}/repo/debug','repo-debug','yast2'),
    ('http://download.opensuse.org/${release}/repo/non-oss','repo-non-oss','yast2'),
    ('http://download.opensuse.org/${release}/repo/oss','repo-oss','yast2'),
    ('http://download.opensuse.org/update/${release}/','repo-update','rpm-md')
)

def is_package_installed(package):
    """Return True if a package is installed on the system.

    :param str package: the deb package to query for.
    :returns: True if the package is installed, False if not.
    """
    with zypper.Cache() as zypper_cache:
        return zypper_cache[package].installed


def install_build_packages(packages):
    unique_packages = set(packages)
    new_packages = []
   
    with zypper.Cache() as zypper_cache:
        for pkg in unique_packages:
            try:
                if not zypper_cache[pkg].installed:
                    new_packages.append(pkg)
            except KeyError as e:
                raise EnvironmentError(
                        "Could not find a required package in "
                        "'build-packages': {}".format(str(e)))
    if new_packages:
        logger.info(
            'Installing build dependencies: %s', ' '.join(new_packages))
        env = os.environ.copy()
        env.update({
            'DEBIAN_FRONTEND': 'noninteractive',
            'DEBCONF_NONINTERACTIVE_SEEN': 'true',
        })
        subprocess.check_call(['sudo', 'zypper','-n','install',
                               '--no-recommends',
                               ] + new_packages, env=env)


class PackageNotFoundError(Exception):

    @property
    def message(self):
        return 'The package "{}" was not found'.format(
            self.package_name)

    def __init__(self, package_name):
        self.package_name = package_name


class UnpackError(Exception):

    @property
    def message(self):
        return 'Error while provisioning "{}"'.format(self.package_name)

    def __init__(self, package_name):
        self.package_name = package_name


class Repo:

    def __init__(self, rootdir, recommends=False,
                 sources=None, project_options=None):
        self.downloaddir = os.path.join(rootdir, 'download')
        self.rootdir = rootdir
        self.recommends = recommends

        if not project_options:
            project_options = snapcraft.ProjectOptions()
        self.repo_cache, self.repo_progress = _setup_zypper(
            rootdir, sources, project_options)

    def get(self, package_names):
        logger.debug("Getting packages: %s" % ",".join(package_names) )
        self.downloaddir = self.repo_cache.rootdir
        logger.debug("changed downloaddir to %s" % self.downloaddir)
        self.repo_cache.download_packages(package_names)

        # TODO: find a way how to exclude base packages from snap
        '''
        # Create the 'partial' subdir too (LP: #1578007).
        os.makedirs(os.path.join(self.downloaddir, 'partial'), exist_ok=True)

        manifest_dep_names = self._manifest_dep_names()

        for name in package_names:
            try:
                self.repo_cache[name].mark_install()
            except KeyError:
                raise PackageNotFoundError(name)

        skipped_essential = []
        skipped_blacklisted = []

        # unmark some base packages here
        for pkg in self.repo_cache:
            # those should be already on each system, it also prevents
            # diving into downloading libc6
            if (pkg.candidate.priority in 'essential' and
               pkg.name not in package_names):
                skipped_essential.append(pkg.name)
                pkg.mark_keep()
                continue
            if (pkg.name in manifest_dep_names and
                    pkg.name not in package_names):
                skipped_blacklisted.append(pkg.name)
                pkg.mark_keep()
                continue

        if skipped_essential:
            print('Skipping priority essential packages:', skipped_essential)
        if skipped_blacklisted:
            print('Skipping blacklisted from manifest packages:',
                  skipped_blacklisted)

        '''

    def unpack(self, rootdir):
        logger.debug("Unpacking in rootdir: %s" % rootdir)
        logger.debug("Downloaddir: %s" % self.downloaddir)
        search_glob = os.path.join(self.downloaddir,'var/cache/zypp/packages', '**', '*.rpm')
        logger.debug("Searching packages in %s" % search_glob)
        odir = os.getcwd()
        logger.debug("changing directory to '%s'" % rootdir)
        os.makedirs(rootdir, exist_ok=True) 
        os.chdir(rootdir)
        for pkg in glob.glob(search_glob,recursive=True):
            logger.debug("Extrating file '%s'" % pkg)
            # TODO needs elegance and error control
            #try:
            rpm2cpio  = subprocess.Popen(['rpm2cpio',pkg],stdout=subprocess.PIPE)
            cpio2disk = subprocess.Popen(['cpio','-idmv'],stdin=rpm2cpio.stdout, stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            rpm2cpio.stdout.close()

            #except subprocess.CalledProcessError as e:
            #    print(e.stdout)
            #    os.chdir(odir)
            #    raise UnpackError(pkg)
        os.chdir(odir)
        _fix_symlinks(rootdir)
        _fix_xml_tools(rootdir)
        _fix_shebangs(rootdir)

    def _manifest_dep_names(self):
        manifest_dep_names = set()

        with open(os.path.abspath(os.path.join(__file__, '..',
                                               'manifest.txt'))) as f:
            for line in f:
                pkg = line.strip()
                if pkg in self.repo_cache:
                    manifest_dep_names.add(pkg)

        return manifest_dep_names


def _get_local_sources_list():

    with zypper.Cache(rootdir="/") as zypp:
        sources = []
        repolist = zypp.repo_list()
        for rep in repolist:
            logger.debug("URL(%s): %s " % (rep['enabled'],rep['url']))
            if int(rep['enabled']) == 1:
                logger.debug(" - Adding")
                sources.append(
		    [
                        rep['url'],
			rep['alias'],
			rep['type']
		    ]
                )
    return sources


def _format_sources_list(sources, project_options, release='tumbleweed'):
    if not sources:
        sources = _DEFAULT_SOURCES

    result = ()

    for src in sources:
        result.append(
            string.Template(src[0]).substitute({
                'release': release
            }),
            src[1],
            src[2]
        )

    return result


def _setup_zypper(rootdir, sources, project_options):

    progress = None

    zypp_dir = os.path.join(rootdir, 'etc', 'zypp')
    logger.debug("Creating zypp_dir: %s" % zypp_dir)
    os.makedirs(os.path.join(rootdir, 'etc', 'zypp'), exist_ok=True)

    if sources:
        release = _get_suse_url()
        logger.debug("Formatting source list (release: %s)" % release)
        sources = _format_sources_list(
            sources, project_options, release)
    else:
        logger.debug("Using local source list")
        sources = _get_local_sources_list()

    zypp = zypper.Cache(rootdir=rootdir)
    for src in sources:
        logger.debug("Adding repo '%s' with url: %s" % (src[1],src[0]))
        zypp.add_repo(url=src[0],alias=src[1],type=src[2])
    
    zypp.refresh()

    return zypp, progress


def _get_suse_url():
    os_str  = platform.linux_distribution()[0]
    version = platform.linux_distribution()[1]

    if os_str == "openSUSE ":
        if float(version) > 20160000:
            return "tumbleweed"
        if float(version) > 42:
            return "distribution/leap/%s" % version

    if os_str == 'SUSE Linux Enterprise Server ':
        logger.error("Support for SUSE Linux Enterprise Server not implemented yes")
        # FIXME: implement for SLES
        pass

    raise NotImplementedError

def _fix_symlinks(debdir):
    '''
    Sometimes debs will contain absolute symlinks (e.g. if the relative
    path would go all the way to root, they just do absolute).  We can't
    have that, so instead clean those absolute symlinks.

    Some unpacked items will also contain suid binaries which we do not want in
    the resulting snap.
    '''
    for root, dirs, files in os.walk(debdir):
        # Symlinks to directories will be in dirs, while symlinks to
        # non-directories will be in files.
        for entry in itertools.chain(files, dirs):
            path = os.path.join(root, entry)
            if os.path.islink(path) and os.path.isabs(os.readlink(path)):
                target = os.path.join(debdir, os.readlink(path)[1:])
                if _skip_link(os.readlink(path)):
                    logger.debug('Skipping {}'.format(target))
                    continue
                if not os.path.exists(target):
                    if not _try_copy_local(path, target):
                        continue
                os.remove(path)
                os.symlink(os.path.relpath(target, root), path)
            elif os.path.exists(path):
                _fix_filemode(path)


def _fix_xml_tools(root):
    xml2_config_path = os.path.join(root, 'usr', 'bin', 'xml2-config')
    if os.path.isfile(xml2_config_path):
        common.run(
            ['sed', '-i', '-e', 's|prefix=/usr|prefix={}/usr|'.
                format(root), xml2_config_path])

    xslt_config_path = os.path.join(root, 'usr', 'bin', 'xslt-config')
    if os.path.isfile(xslt_config_path):
        common.run(
            ['sed', '-i', '-e', 's|prefix=/usr|prefix={}/usr|'.
                format(root), xslt_config_path])


def _fix_filemode(path):
    mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    if mode & 0o4000 or mode & 0o2000:
        logger.warning('Removing suid/guid from {}'.format(path))
        os.chmod(path, mode & 0o1777)


def _fix_shebangs(path):
    """Changes hard coded shebangs for files in _BIN_PATHS to use env."""
    paths = [p for p in _BIN_PATHS if os.path.exists(os.path.join(path, p))]
    for p in [os.path.join(path, p) for p in paths]:
        try:
            common.replace_in_file(p, re.compile(r''),
                               re.compile(r'#!.*python\n'),
                               r'#!/usr/bin/env python\n')
        except PermissionError:
            pass


_skip_list = None


def _skip_link(target):
    global _skip_list
    if not _skip_list:
        output = common.run_output(['rpm', '-ql', 'glibc']).split()
        _skip_list = [i for i in output if 'lib' in i]

    return target in _skip_list


def _try_copy_local(path, target):
    real_path = os.path.realpath(path)
    if os.path.exists(real_path):
        logger.warning(
            'Copying needed target link from the system {}'.format(real_path))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        src = os.readlink(path)
        if os.path.isdir(src):
           logger.warning("Skip copying source '%s' because its a directory" % src)
        else:
           shutil.copyfile(src, target)
        return True
    else:
        logger.warning(
            '{} will be a dangling symlink'.format(path))
        return False
