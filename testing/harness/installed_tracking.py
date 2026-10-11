"""Load the installed release's Python modules without launching its application.

Only production_hub modules are intercepted. No source fallback is permitted:
that would silently test Dev while claiming to test the installed release.
"""
from __future__ import annotations
import hashlib
import importlib.abc
import importlib.util
import plistlib
import sys
from pathlib import Path


def use_installed_release(bundle):
    from PyInstaller.archive.readers import CArchiveReader
    if any(name == 'production_hub' or name.startswith('production_hub.') for name in sys.modules):
        raise RuntimeError('Release must be selected before importing production_hub')
    bundle = Path(bundle).resolve()
    info = plistlib.loads((bundle/'Contents/Info.plist').read_bytes())
    executable = bundle/'Contents/MacOS'/info['CFBundleExecutable']
    archive = CArchiveReader(str(executable)).open_embedded_archive('PYZ.pyz')
    hashes = {}

    class ReleaseModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != 'production_hub' and not fullname.startswith('production_hub.'):
                return None
            if fullname not in archive.toc:
                raise ImportError(f'Installed release has no {fullname}; refusing source fallback')
            return importlib.util.spec_from_loader(fullname,self,is_package=archive.toc[fullname][0]==1)
        def create_module(self, spec):
            return None
        def exec_module(self,module):
            import marshal
            code = archive.extract(module.__name__)
            hashes[module.__name__] = hashlib.sha256(marshal.dumps(code)).hexdigest()
            module.__file__ = str(executable)+'!'+module.__name__
            exec(code,module.__dict__)
    sys.meta_path.insert(0,ReleaseModules())
    return {'kind':'installed-release-code-offline','bundle':str(bundle),'version':info.get('CFBundleShortVersionString'),'executable_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),'loaded_module_sha256':hashes,'physical_camera_test':False}
