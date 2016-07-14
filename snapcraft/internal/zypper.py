
import subprocess
import xml.etree.ElementTree as ET
import locale
import os
import weakref
import logging

logger = logging.getLogger(__name__)

'''
<?xml version='1.0'?>
<stream>
<message type="info">Loading repository data...</message>
<message type="info">Reading installed packages...</message>

<search-result version="0.0">
<solvable-list>
<solvable status="not-installed" name="0ad-data" summary="The Data Files for 0 AD" kind="package"/>
'''

class Package(object):
    def __init__(self,**kwargs):
        self.name       = kwargs['name']
        self.status     = kwargs['status']
        self.summary    = kwargs['summary']
        self.kind       = kwargs['kind']
        
    def installed(self):
        if self.status == "installed":
            return True
        return False

class Cache(object):
    def __init__(self,**kwargs):
        self.rootdir  = kwargs.get('rootdir')

    def _run_zypper_xml(self,*args,**kwargs):
        root_args = []
        skip_sudo = 0
        if self.rootdir:
            root_args = ['--root',self.rootdir]
            if os.access(self.rootdir, os.W_OK):
                skip_sudo = 1

        cmd = ['zypper','--non-interactive','--gpg-auto-import-keys','-x',*root_args,*args]

        if kwargs.get('sudo') == 1 and not skip_sudo:
            cmd.insert(0,'sudo')
            logger.warning("Using sudo to execute the following zypper command:")
            logger.warning(" ".join(cmd))

        # this needs to be done to get predictable output
        # from system calls
        lang = os.environ['LANG']
        os.environ['LANG'] = 'C'
        
        try:
                p = subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,check=True)
        except subprocess.CalledProcessError as e:
            print("Error while execution of command '%s'" % cmd)
            print(e.stdout)
            raise e
            
        os.environ['LANG'] = lang
        return ET.fromstring(p.stdout), p.stdout

    def __enter__(self):
        # Dont't do automatic refresh as it might
        #self.refresh()
        self.read_cache()
        return self

    def read_cache(self):
        self._cache = {}
        self._depcache = None
        self._records = None
        self._list = None
        self._callbacks = {}
        self._weakref = weakref.WeakValueDictionary()
        self._set = set()
        self._fullnameset = set()
        self._changes_count = -1
        self._sorted_set = None

        self.xml_root, self.__zypper_search_output_string = self._run_zypper_xml('search','-t','package')

        s_list = self.xml_root.find("*/solvable-list")
        p_list = s_list.findall("./solvable")
        for pkg in p_list:
            name = pkg.get("name")
            if name:
                self._set.add(name)
                self._cache[name] = { 
                        'name'       : name,
                        'status'     : pkg.get("status"),
                        'summary'    : pkg.get("summary"),
                        'kind'       : pkg.get("kind")
                }


    def __exit__(self,exc_type, exc_val, exc_tb):
        if exc_tb:
            print("traceback:")
            print(exc_tb)

    def __getitem__(self, key):
        """ look like a dictionary (get key) """
        try:
            return self._weakref[key]
        except KeyError:
            if key in self._cache:
                key = str(key)
                pkg = self._weakref[key] = Package(**self._cache[key])
                return pkg
            else:
                raise KeyError('The cache has no package named %r' % key)


    def __iter__(self):
        if self._sorted_set is None:
            self._sorted_set = sorted(self._set)

        for pkgname in self._sorted_set:
            yield self[pkgname]
        raise StopIteration


    def repo_list(self,rootdir=None):
        '''<repo alias="Emulators" name="Emulators (openSUSE_Factory)" type="rpm-md" priority="99" enabled="0" autorefresh="0" gpgcheck="1" repo_gpgcheck="1" pkg_gpgcheck="0" gpgkey="http://.../repodata/repomd.xml.key">'''
        replist, out = self._run_zypper_xml('ls','-u')
        
        self._repolist = replist[0].findall("repo")
        
        result = [] 
        for rep in self._repolist:
            rep_result = {}
            for kw in ('alias','name','type','priority','enabled','autorefresh','gpgcheck','repo_gpgcheck','pkg_gpgcheck','gpgkey'):
                rep_result[kw] = rep.get(kw)
            url_elm = rep.find('url')
            rep_result['url'] = url_elm.text
            
            result.append(rep_result)

        return result

    def open(self):
        pass

    def refresh(self):
        xml, out = self._run_zypper_xml('refresh','-s',sudo=1)

    def add_repo(self,**kwargs):
        xml, out = self._run_zypper_xml('addrepo','-t',kwargs['type'],kwargs['url'],kwargs['alias'],sudo=1)

    def install(self,package_names):
        xml, out = self._run_zypper_xml('install',*package_names,sudo=1)

    def download_packages(self,package_names):
        xml, out = self._run_zypper_xml('install','--download-only',*package_names,sudo=1)

