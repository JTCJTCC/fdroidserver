#!/usr/bin/env python3
#
# scanner.py - part of the FDroid server tools
# Copyright (C) 2010-13, Ciaran Gultnieks, ciaran@ciarang.com
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import imghdr
import json
import os
import re
import sys
import traceback
import zipfile
import yaml
from argparse import ArgumentParser
from collections import namedtuple
from copy import deepcopy
from tempfile import TemporaryDirectory
from pathlib import Path
import logging
import itertools
import urllib.request
from datetime import datetime, timedelta

import requests

from . import _
from . import common
from . import metadata
from .exception import BuildException, VCSException, ConfigurationException
from . import scanner

config = None
options = None

DEFAULT_JSON_PER_BUILD = {'errors': [], 'warnings': [], 'infos': []}  # type: ignore
json_per_build = deepcopy(DEFAULT_JSON_PER_BUILD)

MAVEN_URL_REGEX = re.compile(r"""\smaven\s*(?:{.*?(?:setUrl|url)|\((?:url)?)\s*=?\s*(?:uri)?\(?\s*["']?([^\s"']+)["']?[^})]*[)}]""",
                             re.DOTALL)


SCANNER_CACHE_VERSION = 1


def get_gradle_compile_commands(build):
    compileCommands = ['compile',
                       'provided',
                       'apk',
                       'implementation',
                       'classpath',
                       'api',
                       'compileOnly',
                       'runtimeOnly']
    buildTypes = ['', 'release']
    flavors = ['']
    if build.gradle and build.gradle != ['yes']:
        flavors += build.gradle

    commands = [''.join(c) for c in itertools.product(flavors, buildTypes, compileCommands)]
    return [re.compile(r'\s*' + c, re.IGNORECASE) for c in commands]


def get_embedded_classes(apkfile, depth=0):
    """
    Get the list of Java classes embedded into all DEX files.

    :return: set of Java classes names as string
    """
    if depth > 10:  # zipbomb protection
        return {_('Max recursion depth in ZIP file reached: %s') % apkfile}

    archive_regex = re.compile(r'.*\.(aab|aar|apk|apks|jar|war|xapk|zip)$')
    class_regex = re.compile(r'classes.*\.dex')
    classes = set()

    try:
        with TemporaryDirectory() as tmp_dir, zipfile.ZipFile(apkfile, 'r') as apk_zip:
            for info in apk_zip.infolist():
                # apk files can contain apk files, again
                with apk_zip.open(info) as apk_fp:
                    if zipfile.is_zipfile(apk_fp):
                        classes = classes.union(get_embedded_classes(apk_fp, depth + 1))
                        if not archive_regex.search(info.filename):
                            classes.add(
                                'ZIP file without proper file extension: %s'
                                % info.filename
                            )
                        continue

                with apk_zip.open(info.filename) as fp:
                    file_magic = fp.read(3)
                if file_magic == b'dex':
                    if not class_regex.search(info.filename):
                        classes.add('DEX file with fake name: %s' % info.filename)
                    apk_zip.extract(info, tmp_dir)
                    run = common.SdkToolsPopen(
                        ["dexdump", '{}/{}'.format(tmp_dir, info.filename)],
                        output=False,
                    )
                    classes = classes.union(set(re.findall(r'[A-Z]+((?:\w+\/)+\w+)', run.output)))
    except zipfile.BadZipFile as ex:
        return {_('Problem with ZIP file: %s, error %s') % (apkfile, ex)}

    return classes


# taken from exodus_core
def _exodus_compile_signatures(signatures):
    """
    Compiles the regex associated to each signature, in order to speed up the trackers detection.

    :return: A compiled list of signatures.
    """
    compiled_tracker_signature = []
    try:
        compiled_tracker_signature = [
            re.compile(track.code_signature) for track in signatures
        ]
    except TypeError:
        print("signatures is not iterable")
    return compiled_tracker_signature


def _datetime_now():
    """
    simple warpper for datetime.now to allow mocking it for testing
    """
    return datetime.now().astimezone()


def _scanner_cachedir():
    """
    get `Path` to local cache dir
    """
    if not common.config or "cachedir_scanner" not in common.config:
        raise ConfigurationException("could not load 'cachedir_scanner' config")
    cachedir = Path(config["cachedir_scanner"])
    cachedir.mkdir(exist_ok=True, parents=True)
    return cachedir


class SignatureCacheMalformedException(Exception):
    pass


class SignatureCacheOutdatedException(Exception):
    pass


class SignatureDataController:
    def __init__(self, name, filename):
        self.name = name
        self.filename = filename
        self.cache_outdated_interval = timedelta(days=7)
        self.data = {}

    def check_data_version(self):
        if self.data.get("version") != SCANNER_CACHE_VERSION:
            raise SignatureCacheMalformedException()

    def check_last_updated(self):
        timestamp = self.data.get("timestamp")
        if not timestamp:
            raise SignatureCacheMalformedException()
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError as e:
            raise SignatureCacheMalformedException() from e
        except TypeError as e:
            raise SignatureCacheMalformedException() from e
        if (timestamp + self.cache_outdated_interval) < scanner._datetime_now():
            raise SignatureCacheOutdatedException()

    def load(self):
        try:
            self.load_from_cache()
            self.verify_data()
        except SignatureCacheMalformedException as e:
            self.load_from_defaults()
            self.write_to_cache()

    def load_from_defaults(self):
        sig_file = Path(__file__).absolute().parent / 'data' / 'scanner' / self.filename
        with open(sig_file) as f:
            self.data = json.load(f)

    def load_from_cache(self):
        sig_file = scanner._scanner_cachedir() / self.filename
        if not sig_file.exists():
            raise SignatureCacheMalformedException()
        with open(sig_file) as f:
            self.data = json.load(f)

    def write_to_cache(self):
        sig_file = scanner._scanner_cachedir() / self.filename
        with open(sig_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        logging.debug("write '{}' to cache".format(self.filename))

    def verify_data(self):
        valid_keys = ['timestamp', 'version', 'signatures']
        for k in [x for x in self.data.keys() if x not in valid_keys]:
            del self.data[k]

    # def scan


class ExodusSignatureDataController(SignatureDataController):
    def __init__(self):
        super().__init__('Exodus signatures', 'exodus.yml')

    def fetch_signatures_from_web():
        pass
        # TODO
        # exodus_url = "https://reports.exodus-privacy.eu.org/api/trackers"
        # sigs = {
        #     "signatures": [],
        #     "timestamp": scanner._datetime_now().isoformat(),
        #     "version": SCANNER_CACHE_VERSION,
        # }

        # with urllib.request.urlopen(exodus_url) as f:
        #     data = json.load(f)
        #     for tracker in data["trackers"].values():
        #         sigs["signatures"].append({
        #             "name": tracker["name"],
        #             "binary_signature": tracker["code_signature"],
        #             "network_signature": tracker["network_signature"],
        #             "types": ["tracker", "non-free"]  # right now we assume all trackers in exodus are non-free
        #         })


class ScannerSignatureDataController(SignatureDataController):
    def __init__(self):
        super().__init__('Scanner signatures', 'scanner.json')

    def fetch_signatures_from_web(self):
        url = "https://uniqx.gitlab.io/fdroid-scanner-signatures/sigs.json"
        with urllib.request.urlopen(url) as f:
            data = yaml.safe_load(f)
            # TODO: validate parsed data
            # TODO: error message 'please update fdroidserver/report' when fetching failed due to changes in the data strucutre
            self.data = data


class ScannerTool():
    def __init__(self):
        self.sdcs = [ScannerSignatureDataController()]
        for sdc in self.sdcs:
            sdc.load()
        self.compile_regexes()

    def compile_regexes(self):
        self.regex = {'code_signatures': {}, 'gradle_signatures': {}}
        for sdc in self.sdcs:
            for signame, sigdef in sdc.data.get('signatures', {}).items():
                for sig in sigdef.get('code_signatures', []):
                    self.regex['code_signatures'][sig] = re.compile('.*' + sig, re.IGNORECASE)
                for sig in sigdef.get('gradle_signatures', []):
                    self.regex['gradle_signatures'][sig] = re.compile('.*' + sig, re.IGNORECASE)


# TODO: change this from global instance to dependency injection
SCANNER_TOOL = None


def _get_tool():
    if not scanner.SCANNER_TOOL:
        scanner.SCANNER_TOOL = ScannerTool()
    return scanner.SCANNER_TOOL


# taken from exodus_core
def load_exodus_trackers_signatures():
    """
    Load trackers signatures from the official Exodus database.

    :return: a dictionary containing signatures.
    """
    signatures = []
    exodus_url = "https://reports.exodus-privacy.eu.org/api/trackers"
    r = requests.get(exodus_url, timeout=300)
    data = r.json()
    for e in data['trackers']:
        signatures.append(
            namedtuple('tracker', data['trackers'][e].keys())(
                *data['trackers'][e].values()
            )
        )
    logging.debug('{} trackers signatures loaded'.format(len(signatures)))
    return signatures, scanner._exodus_compile_signatures(signatures)


def scan_binary(apkfile, extract_signatures=None):
    """Scan output of dexdump for known non-free classes."""
    logging.info(_('Scanning APK with dexdump for known non-free classes.'))
    result = get_embedded_classes(apkfile)
    problems = 0
    for classname in result:
        for suspect, regexp in _get_tool().regex['code_signatures'].items():
            if regexp.match(classname):
                logging.debug("Found class '%s'" % classname)
                problems += 1

    if extract_signatures:

        def _detect_tracker(sig, tracker, class_list):
            for clazz in class_list:
                if sig.search(clazz):
                    logging.debug("Found tracker, class {} matching {}".format(clazz, tracker.code_signature))
                    return tracker
            return None

        results = []
        args = [(extract_signatures[1][index], tracker, result)
                for (index, tracker) in enumerate(extract_signatures[0]) if
                len(tracker.code_signature) > 3]

        for res in itertools.starmap(_detect_tracker, args):
            if res:
                results.append(res)

        trackers = [t for t in results if t is not None]
        problems += len(trackers)

    if problems:
        logging.critical("Found problems in %s" % apkfile)
    return problems


def scan_source(build_dir, build=metadata.Build()):
    """Scan the source code in the given directory (and all subdirectories).

    Returns
    -------
    the number of fatal problems encountered.
    """
    count = 0

    allowlisted = [
        'firebase-jobdispatcher',  # https://github.com/firebase/firebase-jobdispatcher-android/blob/master/LICENSE
        'com.firebaseui',  # https://github.com/firebase/FirebaseUI-Android/blob/master/LICENSE
        'geofire-android',  # https://github.com/firebase/geofire-java/blob/master/LICENSE
        'com.yandex.android:authsdk',  # https://github.com/yandexmobile/yandex-login-sdk-android/blob/master/LICENSE.txt
        'com.hypertrack:hyperlog',  # https://github.com/hypertrack/hyperlog-android#license
    ]

    def is_allowlisted(s):
        return any(al in s for al in allowlisted)

    def suspects_found(s):
        for n, r in _get_tool().regex['gradle_signatures'].items():
            if r.match(s) and not is_allowlisted(s):
                yield n

    allowed_repos = [re.compile(r'^https://' + re.escape(repo) + r'/*') for repo in [
        'repo1.maven.org/maven2',  # mavenCentral()
        'jcenter.bintray.com',     # jcenter()
        'jitpack.io',
        'www.jitpack.io',
        'repo.maven.apache.org/maven2',
        'oss.jfrog.org/artifactory/oss-snapshot-local',
        'oss.sonatype.org/content/repositories/snapshots',
        'oss.sonatype.org/content/repositories/releases',
        'oss.sonatype.org/content/groups/public',
        'clojars.org/repo',  # Clojure free software libs
        'repo.clojars.org',  # Clojure free software libs
        's3.amazonaws.com/repo.commonsware.com',  # CommonsWare
        'plugins.gradle.org/m2',  # Gradle plugin repo
        'maven.google.com',  # Google Maven Repo, https://developer.android.com/studio/build/dependencies.html#google-maven
        ]
    ] + [re.compile(r'^file://' + re.escape(repo) + r'/*') for repo in [
        '/usr/share/maven-repo',  # local repo on Debian installs
        ]
    ]

    scanignore = common.getpaths_map(build_dir, build.scanignore)
    scandelete = common.getpaths_map(build_dir, build.scandelete)

    scanignore_worked = set()
    scandelete_worked = set()

    def toignore(path_in_build_dir):
        for k, paths in scanignore.items():
            for p in paths:
                if path_in_build_dir.startswith(p):
                    scanignore_worked.add(k)
                    return True
        return False

    def todelete(path_in_build_dir):
        for k, paths in scandelete.items():
            for p in paths:
                if path_in_build_dir.startswith(p):
                    scandelete_worked.add(k)
                    return True
        return False

    def ignoreproblem(what, path_in_build_dir):
        """No summary.

        Parameters
        ----------
        what: string
          describing the problem, will be printed in log messages
        path_in_build_dir
          path to the file relative to `build`-dir

        Returns
        -------
        0 as we explicitly ignore the file, so don't count an error
        """
        msg = ('Ignoring %s at %s' % (what, path_in_build_dir))
        logging.info(msg)
        if json_per_build is not None:
            json_per_build['infos'].append([msg, path_in_build_dir])
        return 0

    def removeproblem(what, path_in_build_dir, filepath):
        """No summary.

        Parameters
        ----------
        what: string
          describing the problem, will be printed in log messages
        path_in_build_dir
          path to the file relative to `build`-dir
        filepath
          Path (relative to our current path) to the file

        Returns
        -------
        0 as we deleted the offending file
        """
        msg = ('Removing %s at %s' % (what, path_in_build_dir))
        logging.info(msg)
        if json_per_build is not None:
            json_per_build['infos'].append([msg, path_in_build_dir])
        try:
            os.remove(filepath)
        except FileNotFoundError:
            # File is already gone, nothing to do.
            # This can happen if we find multiple problems in one file that is setup for scandelete
            # I.e. build.gradle files containig multiple unknown maven repos.
            pass
        return 0

    def warnproblem(what, path_in_build_dir):
        """No summary.

        Parameters
        ----------
        what: string
          describing the problem, will be printed in log messages
        path_in_build_dir
          path to the file relative to `build`-dir

        Returns
        -------
        0, as warnings don't count as errors
        """
        if toignore(path_in_build_dir):
            return 0
        logging.warning('Found %s at %s' % (what, path_in_build_dir))
        if json_per_build is not None:
            json_per_build['warnings'].append([what, path_in_build_dir])
        return 0

    def handleproblem(what, path_in_build_dir, filepath):
        """Dispatches to problem handlers (ignore, delete, warn).

        Or returns 1 for increasing the error count.

        Parameters
        ----------
        what: string
          describing the problem, will be printed in log messages
        path_in_build_dir
          path to the file relative to `build`-dir
        filepath
          Path (relative to our current path) to the file

        Returns
        -------
        0 if the problem was ignored/deleted/is only a warning, 1 otherwise
        """
        if toignore(path_in_build_dir):
            return ignoreproblem(what, path_in_build_dir)
        if todelete(path_in_build_dir):
            return removeproblem(what, path_in_build_dir, filepath)
        if 'src/test' in path_in_build_dir or '/test/' in path_in_build_dir:
            return warnproblem(what, path_in_build_dir)
        if options and 'json' in vars(options) and options.json:
            json_per_build['errors'].append([what, path_in_build_dir])
        if options and (options.verbose or not ('json' in vars(options) and options.json)):
            logging.error('Found %s at %s' % (what, path_in_build_dir))
        return 1

    def is_executable(path):
        return os.path.exists(path) and os.access(path, os.X_OK)

    textchars = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7f})

    def is_binary(path):
        d = None
        with open(path, 'rb') as f:
            d = f.read(1024)
        return bool(d.translate(None, textchars))

    # False positives patterns for files that are binary and executable.
    safe_paths = [re.compile(r) for r in [
        r".*/drawable[^/]*/.*\.png$",  # png drawables
        r".*/mipmap[^/]*/.*\.png$",    # png mipmaps
        ]
    ]

    def is_image_file(path):
        if imghdr.what(path) is not None:
            return True

    def safe_path(path_in_build_dir):
        for sp in safe_paths:
            if sp.match(path_in_build_dir):
                return True
        return False

    gradle_compile_commands = get_gradle_compile_commands(build)

    def is_used_by_gradle(line):
        return any(command.match(line) for command in gradle_compile_commands)

    # Iterate through all files in the source code
    for root, dirs, files in os.walk(build_dir, topdown=True):

        # It's topdown, so checking the basename is enough
        for ignoredir in ('.hg', '.git', '.svn', '.bzr'):
            if ignoredir in dirs:
                dirs.remove(ignoredir)

        for curfile in files:

            if curfile in ['.DS_Store']:
                continue

            # Path (relative) to the file
            filepath = os.path.join(root, curfile)

            if os.path.islink(filepath):
                continue

            path_in_build_dir = os.path.relpath(filepath, build_dir)

            if curfile in ('gradle-wrapper.jar', 'gradlew', 'gradlew.bat'):
                removeproblem(curfile, path_in_build_dir, filepath)
            elif curfile.endswith('.apk'):
                removeproblem(_('Android APK file'), path_in_build_dir, filepath)

            elif curfile.endswith('.a'):
                count += handleproblem(_('static library'), path_in_build_dir, filepath)
            elif curfile.endswith('.aar'):
                count += handleproblem(_('Android AAR library'), path_in_build_dir, filepath)
            elif curfile.endswith('.class'):
                count += handleproblem(_('Java compiled class'), path_in_build_dir, filepath)
            elif curfile.endswith('.dex'):
                count += handleproblem(_('Android DEX code'), path_in_build_dir, filepath)
            elif curfile.endswith('.gz'):
                count += handleproblem(_('gzip file archive'), path_in_build_dir, filepath)
            # We use a regular expression here to also match versioned shared objects like .so.0.0.0
            elif re.match(r'.*\.so(\..+)*$', curfile):
                count += handleproblem(_('shared library'), path_in_build_dir, filepath)
            elif curfile.endswith('.zip'):
                count += handleproblem(_('ZIP file archive'), path_in_build_dir, filepath)
            elif curfile.endswith('.jar'):
                for name in suspects_found(curfile):
                    count += handleproblem('usual suspect \'%s\'' % name, path_in_build_dir, filepath)
                count += handleproblem(_('Java JAR file'), path_in_build_dir, filepath)

            elif curfile.endswith('.java'):
                if not os.path.isfile(filepath):
                    continue
                with open(filepath, 'r', errors='replace') as f:
                    for line in f:
                        if 'DexClassLoader' in line:
                            count += handleproblem('DexClassLoader', path_in_build_dir, filepath)
                            break

            elif curfile.endswith('.gradle') or curfile.endswith('.gradle.kts'):
                if not os.path.isfile(filepath):
                    continue
                with open(filepath, 'r', errors='replace') as f:
                    lines = f.readlines()
                for i, line in enumerate(lines):
                    if is_used_by_gradle(line):
                        for name in suspects_found(line):
                            count += handleproblem("usual suspect \'%s\'" % (name),
                                                   path_in_build_dir, filepath)
                noncomment_lines = [line for line in lines if not common.gradle_comment.match(line)]
                no_comments = re.sub(r'/\*.*?\*/', '', ''.join(noncomment_lines), flags=re.DOTALL)
                for url in MAVEN_URL_REGEX.findall(no_comments):
                    if not any(r.match(url) for r in allowed_repos):
                        count += handleproblem('unknown maven repo \'%s\'' % url, path_in_build_dir, filepath)

            elif os.path.splitext(path_in_build_dir)[1] in ['', '.bin', '.out', '.exe']:
                if is_binary(filepath):
                    count += handleproblem('binary', path_in_build_dir, filepath)

            elif is_executable(filepath):
                if is_binary(filepath) and not (safe_path(path_in_build_dir) or is_image_file(filepath)):
                    warnproblem(_('executable binary, possibly code'), path_in_build_dir)

    for p in scanignore:
        if p not in scanignore_worked:
            logging.error(_('Unused scanignore path: %s') % p)
            count += 1

    for p in scandelete:
        if p not in scandelete_worked:
            logging.error(_('Unused scandelete path: %s') % p)
            count += 1

    return count


def main():
    global config, options, json_per_build

    # Parse command line...
    parser = ArgumentParser(
        usage="%(prog)s [options] [(APPID[:VERCODE] | path/to.apk) ...]"
    )
    common.setup_global_opts(parser)
    parser.add_argument("appid", nargs='*', help=_("application ID with optional versionCode in the form APPID[:VERCODE]"))
    parser.add_argument(
        "--exodus",
        action="store_true",
        help="Use tracker scanner from Exodus project (requires internet)",
    )
    parser.add_argument("-f", "--force", action="store_true", default=False,
                        help=_("Force scan of disabled apps and builds."))
    parser.add_argument("--json", action="store_true", default=False,
                        help=_("Output JSON to stdout."))
    metadata.add_metadata_arguments(parser)
    options = parser.parse_args()
    metadata.warnings_action = options.W

    json_output = dict()
    if options.json:
        if options.verbose:
            logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
        else:
            logging.getLogger().setLevel(logging.ERROR)

    config = common.read_config(options)

    probcount = 0

    exodus = []
    if options.exodus:
        exodus = load_exodus_trackers_signatures()

    appids = []
    for apk in options.appid:
        if os.path.isfile(apk):
            count = scanner.scan_binary(apk, exodus)
            if count > 0:
                logging.warning(
                    _('Scanner found {count} problems in {apk}:').format(
                        count=count, apk=apk
                    )
                )
                probcount += count
        else:
            appids.append(apk)

    if not appids:
        return

    # Read all app and srclib metadata

    allapps = metadata.read_metadata()
    apps = common.read_app_args(appids, allapps, True)

    build_dir = 'build'
    if not os.path.isdir(build_dir):
        logging.info("Creating build directory")
        os.makedirs(build_dir)
    srclib_dir = os.path.join(build_dir, 'srclib')
    extlib_dir = os.path.join(build_dir, 'extlib')

    for appid, app in apps.items():

        json_per_appid = dict()

        if app.Disabled and not options.force:
            logging.info(_("Skipping {appid}: disabled").format(appid=appid))
            json_per_appid['disabled'] = json_per_build['infos'].append('Skipping: disabled')
            continue

        try:
            if app.RepoType == 'srclib':
                build_dir = os.path.join('build', 'srclib', app.Repo)
            else:
                build_dir = os.path.join('build', appid)

            if app.get('Builds'):
                logging.info(_("Processing {appid}").format(appid=appid))
                # Set up vcs interface and make sure we have the latest code...
                vcs = common.getvcs(app.RepoType, app.Repo, build_dir)
            else:
                logging.info(_("{appid}: no builds specified, running on current source state")
                             .format(appid=appid))
                json_per_build = deepcopy(DEFAULT_JSON_PER_BUILD)
                json_per_appid['current-source-state'] = json_per_build
                count = scan_source(build_dir)
                if count > 0:
                    logging.warning(_('Scanner found {count} problems in {appid}:')
                                    .format(count=count, appid=appid))
                    probcount += count
                app['Builds'] = []

            for build in app.get('Builds', []):
                json_per_build = deepcopy(DEFAULT_JSON_PER_BUILD)
                json_per_appid[build.versionCode] = json_per_build

                if build.disable and not options.force:
                    logging.info("...skipping version %s - %s" % (
                        build.versionName, build.get('disable', build.commit[1:])))
                    continue

                logging.info("...scanning version " + build.versionName)
                # Prepare the source code...
                common.prepare_source(vcs, app, build,
                                      build_dir, srclib_dir,
                                      extlib_dir, False)

                count = scan_source(build_dir, build)
                if count > 0:
                    logging.warning(_('Scanner found {count} problems in {appid}:{versionCode}:')
                                    .format(count=count, appid=appid, versionCode=build.versionCode))
                    probcount += count

        except BuildException as be:
            logging.warning('Could not scan app %s due to BuildException: %s' % (
                appid, be))
            probcount += 1
        except VCSException as vcse:
            logging.warning('VCS error while scanning app %s: %s' % (appid, vcse))
            probcount += 1
        except Exception:
            logging.warning('Could not scan app %s due to unknown error: %s' % (
                appid, traceback.format_exc()))
            probcount += 1

        for k, v in json_per_appid.items():
            if len(v['errors']) or len(v['warnings']) or len(v['infos']):
                json_output[appid] = json_per_appid
                break

    logging.info(_("Finished"))
    if options.json:
        print(json.dumps(json_output))
    else:
        print(_("%d problems found") % probcount)


if __name__ == "__main__":
    main()
