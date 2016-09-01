import logging
import os
import sys
import re
import tempfile
import shutil
import glob
import textwrap
import runpy
from abc import ABCMeta, abstractmethod

from command import extract_dist
from pyp2rpm import archive
from pyp2rpm.dependency_parser import deps_from_pyp_format, deps_from_pydit_json
from pyp2rpm.exceptions import VirtualenvFailException
from pyp2rpm.package_data import PackageData
from pyp2rpm.package_getters import get_url
from pyp2rpm import settings
from pyp2rpm import utils
try:
    from pyp2rpm import virtualenv
except ImportError:
    virtualenv = None

logger = logging.getLogger(__name__)


def current_interpreter_run(setup_py, *args):
    """Runs given setup.py script using current python interpreter."""
    dirname = os.path.dirname(setup_py)
    filename = os.path.basename(setup_py)
    if filename.endswith('py'):
        filename = filename[:-3]

    with utils.ChangeDir(dirname):
        sys.path.insert(0, dirname)
        sys.argv[1:] = args
        runpy.run_module(filename, run_name='__main__', alter_sys=True)


def pypi_metadata_extension(extraction_fce):
    """Extracts data from PyPI and merges them with data from extraction method."""

    def inner(self, client=None):
        data = extraction_fce(self)
        try:
            if client is None:
                raise ValueError("Client is None.")
            release_data = client.release_data(self.name, self.version)
        except:
            logger.warning('Some kind of error while communicating with client: {0}.'.format(
                client), exc_info=True)
            return data

        url, md5_digest = get_url(client, self.name, self.version)
        data_dict = {'url': url, 'md5': md5_digest}

        for data_field in settings.PYPI_USABLE_DATA:
            data_dict[data_field] = release_data.get(data_field, '')

        # we usually get better license representation from trove classifiers
        data_dict["license"] = utils.license_from_trove(release_data.get('classifiers', ''))
        data.set_from(data_dict, update=True)
        return data
    return inner


def venv_metadata_extension(extraction_fce):
    """Extracts specific metadata from virtualenv object, merges them with data
    from given extraction method.
    """

    def inner(self):
        data = extraction_fce(self)
        if virtualenv is None or not self.venv:
            logger.debug("Skipping virtualenv metadata extraction.")
            return data

        temp_dir = tempfile.mkdtemp()
        try:
            extractor = virtualenv.VirtualEnv(self.name, temp_dir,
                                              self.name_convertor,
                                              self.base_python_version)
            data.set_from(extractor.get_venv_data, update=True)
        except VirtualenvFailException as e:
            logger.error("{}, skipping virtualenv metadata extraction.".format(e))
        finally:
            shutil.rmtree(temp_dir)
        return data
    return inner


def process_description(description_fce):
    """Removes special character delimiters, titles 
    and wraps paragraphs.
    """
    def inner(description):
                            # multiple whitespaces
        clear_description = re.sub(r'\s+', ' ',
                            # general URLs
                            re.sub(r'\w+:\/{2}[\d\w-]+(\.[\d\w-]+)*(?:(?:\/[^\s/]*))*', '',
                            # delimiters
                            re.sub('(#|-|=|~|`)*', '',
                            # very short lines, typically titles
                            re.sub('((\r?\n)|^).{0,8}((\r?\n)|$)', '',
                            # PyPI's version and downloads tags
                            re.sub('((\r*.. image::|:target:) https?|(:align:|:alt:))[^\n]*\n', '',
                                description_fce(description))))))
        return ' '.join(textwrap.wrap(clear_description, 80))
    return inner


class LocalMetadataExtractor(object):

    """Abstract base class for metadata extractors, does not provide
    implementation of main method to extract data.
    """

    __metaclass__ = ABCMeta

    def __init__(self, local_file, name, name_convertor, version,
                 rpm_name=None, venv=True,
                 base_python_version=settings.DEFAULT_PYTHON_VERSION,
                 metadata_extension=False):
        self.local_file = local_file
        self.archive = archive.Archive(local_file)
        self.name = name
        self.name_convertor = name_convertor
        self.version = version
        self.rpm_name = rpm_name
        self.venv = venv
        self.base_python_version = base_python_version
        self.metadata_extension = metadata_extension

    def name_convert_deps_list(self, deps_list):
        for dep in deps_list:
            dep[1] = self.name_convertor.rpm_name(dep[1], settings.DEFAULT_PYTHON_VERSION)

        return deps_list

    @property
    @abstractmethod
    def runtime_deps(self):
        pass

    @property
    @abstractmethod
    def build_deps(self):
        pass

    @property
    @abstractmethod
    def py_modules(self):
        pass

    @property
    @abstractmethod
    def scripts(self):
        pass

    @property
    @abstractmethod
    def home_page(self):
        pass

    @property
    @abstractmethod
    def description(self):
        pass

    @property
    @abstractmethod
    def summary(self):
        pass

    @property
    @abstractmethod
    def license(self):
        pass

    @property
    def has_pth(self):
        """Figure out if package has pth file """
        return "." in self.name

    @property
    def has_extension(self):
        """Finds out whether the packages has binary extension.
        Returns:
            True if the package has a binary extension, False otherwise
        """
        return self.archive.has_file_with_suffix(settings.EXTENSION_SUFFIXES)

    @property
    @abstractmethod
    def has_test_suite(self):
        pass

    @property
    @abstractmethod
    def versions_from_archive(self):
        pass

    @property
    @abstractmethod
    def doc_files(self):
        pass

    @pypi_metadata_extension
    @venv_metadata_extension
    def extract_data(self):
        """Extracts data from archive.
        Returns:
            PackageData object containing the extracted data.
        """
        data = PackageData(self.local_file,
                           self.name,
                           self.name_convertor.rpm_name(self.name)
                           if self.rpm_name is None else self.rpm_name,
                           self.version)

        with self.archive:
            data.set_from(self.data_from_archive)

        if "scripts" in data.data:
            setattr(data, "scripts", utils.remove_major_minor_suffix(data.data['scripts']))
        # for example nose has attribute `packages` but instead of name listing the pacakges
        # is using function to find them, that makes data.packages an empty set
        if data.packages in ("TODO:", set()):
            data.packages = set([data.name])

        return data

    @staticmethod
    def separate_license_files(doc_files):
        other = [doc for doc in doc_files if all(s not in doc.lower() for s in
                                                 settings.LICENSE_FILES)]
        licenses = [doc for doc in doc_files if any(s in doc.lower() for s in
                                                    settings.LICENSE_FILES)]
        return other, licenses

    @property
    def data_from_archive(self):
        """Returns all metadata extractable from the archive.
        Returns:
            dictionary containing metadata extracted from the archive
        """
        archive_data = {}

        archive_data['runtime_deps'] = self.runtime_deps
        archive_data['build_deps'] = [['BuildRequires', 'python2-devel']] + self.build_deps

        archive_data['py_modules'] = self.py_modules
        archive_data['scripts'] = self.scripts

        archive_data['home_page'] = self.home_page
        archive_data['description'] = self.description
        archive_data['summary'] = self.summary

        archive_data['license'] = self.license
        archive_data['has_pth'] = self.has_pth
        archive_data['has_extension'] = self.has_extension
        archive_data['has_test_suite'] = self.has_test_suite

        py_vers = self.versions_from_archive
        archive_data['base_python_version'] = py_vers[0] if py_vers \
            else settings.DEFAULT_PYTHON_VERSION
        archive_data['python_versions'] = py_vers[1:] if py_vers \
            else [settings.DEFAULT_ADDITIONAL_VERSION]

        (archive_data['doc_files'],
         archive_data['doc_license']) = self.separate_license_files(self.doc_files)

        return archive_data


class SetupPyMetadataExtractor(LocalMetadataExtractor):
    """Class to extract metadata from setup.py using custom extract_dist command."""

    def __init__(self, *args, **kwargs):
        super(SetupPyMetadataExtractor, self).__init__(*args, **kwargs)

        temp_dir = tempfile.mkdtemp()
        try:
            with self.archive as a:
                a.extract_all(directory=temp_dir)
                try:
                    setup_py = glob.glob(temp_dir + "/{0}*/".format(self.name) + 'setup.py')[0]
                except IndexError:
                    sys.stderr.write(
                        "setup.py not found, maybe local_file is not proper source archive.\n")
                    raise SystemExit(3)
                current_interpreter_run(setup_py, '--quiet', '--command-packages', 'command',
                                        'extract_dist')

                self.distribution = extract_dist.extract_dist.class_dist
        finally:
            shutil.rmtree(temp_dir)

    @property
    def runtime_deps(self):  # install_requires
        """Returns list of runtime dependencies of the package specified in setup.py.

        Dependencies are in RPM SPECFILE format - see dependency_to_rpm() for details,
        but names are already
        transformed according to current distro.

        Returns:
            list of runtime dependencies of the package
        """
        install_requires = self.distribution.install_requires
        if self.distribution.entry_points and 'setuptools' not in install_requires:
            install_requires.append('setuptools')  # entrypoints

        return self.name_convert_deps_list(deps_from_pyp_format(install_requires, runtime=True))

    @property
    def build_deps(self):  # setup_requires + tests_require
        """Same as runtime_deps, but build dependencies. Test requires
        are included only if package contains test suite.

        Returns:
            list of build dependencies of the package
        """
        build_requires = self.distribution.setup_requires + self.distribution.tests_require
        if 'setuptools' not in build_requires:
            build_requires.append('setuptools')
        return self.name_convert_deps_list(deps_from_pyp_format(
            build_requires, runtime=False))

    @property
    def has_packages(self):
        return self.distribution.packages != set()

    @property
    def packages(self):
        if self.has_packages:
            packages = [package.split('.', 1)[0]
                        for package in self.distribution.packages]
            return set(packages)

    @property
    def py_modules(self):
        return set(self.distribution.py_modules)

    @property
    def scripts(self):
        direct = self.distribution.scripts or []
        transformed = []
        if self.distribution.entry_points:
            scripts = self.distribution.entry_points.get('console_scripts', [])
            # handle the case for 'console_scripts' = [ 'a = b' ]
            for script in scripts:
                equal_sign = script.find('=')
                if equal_sign == -1:
                    transformed.append(script)
                else:
                    transformed.append(script[0:equal_sign].strip())
        return set([os.path.basename(t) for t in transformed + direct])

    @property
    def home_page(self):
        return self.distribution.url

    @property
    @process_description
    def long_description(self):
        return self.distribution.long_description

    @property
    def description(self):
        """Shorten description on first newline after approx 10 lines"""
        cut = self.long_description.find('\n', 80 * 8)
        if cut > -1:
            return self.long_description[:cut] + '\n...'
        else:
            return self.long_description

    @property
    def summary(self):
        return self.distribution.description

    @property
    def classifiers(self):
        return self.distribution.classifiers

    @property
    def license(self):
        return self.distribution.license

    @property
    def versions_from_archive(self):
        return utils.versions_from_trove(self.distribution.classifiers)

    @property
    def has_bundled_egg_info(self):
        """Finds out if there is a bundled .egg-info dir in the archive.
        Returns:
            True if the archive contains bundled .egg-info directory, False otherwise
        """
        return self.archive.has_file_with_suffix('.egg-info')

    @property
    def has_test_suite(self):
        """Finds out whether the package contains setup.py test suite.
        Returns:
            True if the package contains setup.py test suite, False otherwise
        """
        return self.distribution.test_suite is not None or self.distribution.tests_require != []

    @property
    def doc_files(self):
        """Returns list of doc files that should be used for %doc in specfile.
        Returns:
            List of doc files from the archive - only basenames, not full paths.
        """
        doc_files = []
        for doc_file_re in settings.DOC_FILES_RE:
            doc_files.extend(
                self.archive.get_files_re(doc_file_re, ignorecase=True))
        return ['/'.join(x.split('/')[1:]) for x in doc_files]

    @property
    def sphinx_dir(self):
        """Returns directory with sphinx documentation, if there is such.
        Returns:
            Full path to sphinx documentation dir inside the archive, or None if there is no such.
        """
        sphinx_dir = None

        # search for sphinx dir doc/ or docs/ under the first directory in
        # archive (e.g. spam-1.0.0/doc)
        candidate_dirs = self.archive.get_directories_re(
            settings.SPHINX_DIR_RE, full_path=True)
        for d in candidate_dirs:  # search for conf.py in the dirs (TODO: what if more are found?)
            contains_conf_py = len(self.archive.get_files_re(
                r'{0}/conf.py'.format(re.escape(d)), full_path=True)) > 0
            if contains_conf_py:
                sphinx_dir = d

        return sphinx_dir

    @property
    def data_from_archive(self):
        """Appends setup.py specific metadata to archive_data."""

        archive_data = super(SetupPyMetadataExtractor, self).data_from_archive

        archive_data['has_packages'] = self.has_packages
        archive_data['packages'] = self.packages
        archive_data['has_bundled_egg_info'] = self.has_bundled_egg_info
        sphinx_dir = self.sphinx_dir
        if sphinx_dir:
            archive_data['sphinx_dir'] = "/".join(sphinx_dir.split("/")[1:])
            archive_data['build_deps'].append(
                ['BuildRequires', 'python-sphinx'])

        return archive_data


class WheelMetadataExtractor(LocalMetadataExtractor):
    """Class to extract metadata from wheel archive"""

    @property
    def json_metadata(self):
        if not hasattr(self, '_json_metadata'):
            self._json_metadata = self.archive.json_wheel_metadata
        return self._json_metadata

    def get_requires(self, requires_types):
        "Extracts requires of given types from metadata file, filter windows specific requires"
        # TODO extras?
        if not isinstance(requires_types, list):
            requires_types = list(requires_types)
        extracted_requires = []
        for requires_name in requires_types:
            for requires in self.json_metadata.get(requires_name, []):
                if 'win' in requires.get('environment', {}):
                    continue
                extracted_requires.extend(requires['requires'])
        return extracted_requires

    @property
    def runtime_deps(self):
        run_requires = self.get_requires(['run_requires', 'meta_requires'])
        if 'setuptools' not in run_requires:
            run_requires.append('setuptools')
        return self.name_convert_deps_list(deps_from_pydit_json(run_requires))

    @property
    def build_deps(self):
        build_requires = self.get_requires(['build_requires', 'test_requires'])
        if 'setuptools' not in build_requires:
            build_requires.append('setuptools')
        return self.name_convert_deps_list(deps_from_pydit_json(build_requires, runtime=False))

    @property
    def py_modules(self):
        return self.archive.record.get('modules')

    @property
    def scripts(self):
        return self.archive.record.get('scripts', [])

    @property
    def home_page(self):
        urls = [url for url in self.json_metadata.get('extensions', {})
                                                 .get('python.details', {})
                                                 .get('project_urls', {}).values()]
        if urls:
            return urls[0]

    @property
    @process_description
    def description(self):
        return self.archive.wheel_description()

    @property
    def summary(self):
        return self.json_metadata.get('summary', None)

    @property
    def classifiers(self):
        return self.json_metadata.get('classifiers', [])

    @property
    def license(self):
        return self.json_metadata.get('license', None)

    @property
    def versions_from_archive(self):
        return utils.versions_from_trove(self.classifiers)

    @property
    def has_test_suite(self):
        return self.json_metadata.get('test_requires', False) is not False

    @property
    def doc_files(self):
        return set([doc for doc in self.json_metadata.get('extensions', {})
                                                     .get('python.details', {})
                                                     .get('document_names', {}).values()])
