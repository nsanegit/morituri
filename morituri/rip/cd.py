# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4

# Morituri - for those about to RIP

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of morituri.
#
# morituri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# morituri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with morituri.  If not, see <http://www.gnu.org/licenses/>.

import os
import math

import gobject
gobject.threads_init()

from morituri.common import logcommand, common, accurip, gstreamer
from morituri.common import drive, program, cache
from morituri.result import result
from morituri.program import cdrdao, cdparanoia
from morituri.rip import common as rcommon

from morituri.extern.task import task


MAX_TRIES = 5


class Rip(logcommand.LogCommand):
    summary = "rip CD"

    # see morituri.common.program.Program.getPath for expansion
    description = """
Rips a CD.

%s

Paths to track files referenced in .cue and .m3u files will be made
relative to the directory of the disc files.

All files will be created relative to the given output directory.
Log files will log the path to tracks relative to this directory.
""" % rcommon.TEMPLATE_DESCRIPTION

    def addOptions(self):
        loggers = result.getLoggers().keys()

        self.parser.add_option('-L', '--logger',
            action="store", dest="logger",
            default='morituri',
            help="logger to use "
                "(default '%default', choose from '" +
                    "', '".join(loggers) + "')")
        # FIXME: get from config
        self.parser.add_option('-o', '--offset',
            action="store", dest="offset",
            help="sample read offset (defaults to configured value, or 0)")
        self.parser.add_option('-O', '--output-directory',
            action="store", dest="output_directory",
            help="output directory; will be included in file paths in result "
                "files "
                "(defaults to absolute path to current directory; set to "
                "empty if you want paths to be relative instead) ")
        self.parser.add_option('-W', '--working-directory',
            action="store", dest="working_directory",
            help="working directory; morituri will change to this directory "
                "and files will be created relative to it when not absolute ")
        # FIXME: have a cache of these pickles somewhere
        self.parser.add_option('-T', '--toc-pickle',
            action="store", dest="toc_pickle",
            help="pickle to use for reading and writing the TOC")

        rcommon.addTemplate(self)

        self.parser.add_option('-R', '--release-id',
            action="store", dest="release_id",
            help="MusicBrainz release id to match to (if there are multiple)")

        default = 'flac'

        # here to avoid import gst eating our options
        from morituri.common import encode

        self.parser.add_option('', '--profile',
            action="store", dest="profile",
            help="profile for encoding (default '%s', choices '%s')" % (
                default, "', '".join(encode.PROFILES.keys())),
            default=default)
        self.parser.add_option('-U', '--unknown',
            action="store_true", dest="unknown",
            help="whether to continue ripping if the CD is unknown (%default)",
            default=False)

    def handleOptions(self, options):
        options.track_template = options.track_template.decode('utf-8')
        options.disc_template = options.disc_template.decode('utf-8')

        if options.offset is None:
            info = drive.getDeviceInfo(self.parentCommand.options.device)
            if info:
                try:
                    options.offset = self.getRootCommand(
                        ).config.getReadOffset(*info)
                    self.stdout.write("Using configured read offset %d\n" %
                        options.offset)
                except KeyError:
                    pass

        if options.offset is None:
            options.offset = 0
            self.stdout.write("Using fallback read offset %d\n" %
                        options.offset)
        if self.options.output_directory is None:
            self.options.output_directory = os.getcwd()

    def do(self, args):
        prog = program.Program(record=self.getRootCommand().record,
            stdout=self.stdout)
        runner = task.SyncRunner()

        def function(r, t):
            r.run(t)

        # if the device is mounted (data session), unmount it
        device = self.parentCommand.options.device
        self.stdout.write('Checking device %s\n' % device)

        prog.setWorkingDirectory(self.options.working_directory)
        prog.loadDevice(device)
        prog.unmountDevice(device)

        version = None

        # first, read the normal TOC, which is fast
        ptoc = cache.Persister(self.options.toc_pickle or None)
        if not ptoc.object:
            tries = 0
            while True:
                tries += 1
                t = cdrdao.ReadTOCTask(device=device)
                try:
                    function(runner, t)
                    break
                except:
                    if tries > 3:
                        raise
                    self.debug('failed to read TOC after %d tries, retrying' % tries)

            version = t.tasks[1].parser.version
            from pkg_resources import parse_version as V
            # we've built a cdrdao 1.2.3rc2 modified package with the patch
            if V(version) < V('1.2.3rc2p1'):
                self.stdout.write('Warning: cdrdao older than 1.2.3 has a '
                    'pre-gap length bug.\n'
                    'See http://sourceforge.net/tracker/?func=detail'
                    '&aid=604751&group_id=2171&atid=102171\n')
            ptoc.persist(t.table)
        ittoc = ptoc.object
        assert ittoc.hasTOC()

        # already show us some info based on this
        prog.getRipResult(ittoc.getCDDBDiscId())
        self.stdout.write("CDDB disc id: %s\n" % ittoc.getCDDBDiscId())
        mbdiscid = ittoc.getMusicBrainzDiscId()
        self.stdout.write("MusicBrainz disc id %s\n" % mbdiscid)

        self.stdout.write("MusicBrainz lookup URL %s\n" %
            ittoc.getMusicBrainzSubmitURL())

        prog.metadata = prog.getMusicBrainz(ittoc, mbdiscid,
            release=self.options.release_id)

        if not prog.metadata:
            # fall back to FreeDB for lookup
            cddbid = ittoc.getCDDBValues()
            cddbmd = prog.getCDDB(cddbid)
            if cddbmd:
                self.stdout.write('FreeDB identifies disc as %s\n' % cddbmd)

            if not self.options.unknown:
                prog.ejectDevice(device)
                return -1

        # now, read the complete index table, which is slower
        itable = prog.getTable(runner, ittoc.getCDDBDiscId(), device)

        assert itable.getCDDBDiscId() == ittoc.getCDDBDiscId(), \
            "full table's id %s differs from toc id %s" % (
                itable.getCDDBDiscId(), ittoc.getCDDBDiscId())
        assert itable.getMusicBrainzDiscId() == ittoc.getMusicBrainzDiscId(), \
            "full table's mb id %s differs from toc id mb %s" % (
            itable.getMusicBrainzDiscId(), ittoc.getMusicBrainzDiscId())
        assert itable.getAccurateRipURL() == ittoc.getAccurateRipURL(), \
            "full table's AR URL %s differs from toc AR URL %s" % (
            itable.getAccurateRipURL(), ittoc.getAccurateRipURL())

        prog.outdir = self.options.output_directory
        prog.outdir = prog.outdir.decode('utf-8')
        # here to avoid import gst eating our options
        from morituri.common import encode
        profile = encode.PROFILES[self.options.profile]()

        # result

        prog.result.cdrdaoVersion = version
        prog.result.cdparanoiaVersion = cdparanoia.getCdParanoiaVersion()
        info = drive.getDeviceInfo(self.parentCommand.options.device)
        if info:
            try:
                prog.result.cdparanoiaDefeatsCache = self.getRootCommand(
                    ).config.getDefeatsCache(*info)
            except KeyError, e:
                self.debug('Got key error: %r' % (e, ))
        prog.result.offset = int(self.options.offset)
        prog.result.artist = prog.metadata and prog.metadata.artist \
            or 'Unknown Artist'
        prog.result.title = prog.metadata and prog.metadata.title \
            or 'Unknown Title'
        # cdio is optional for now
        try:
            import cdio
            _, prog.result.vendor, prog.result.model, prog.result.release = \
                cdio.Device(device).get_hwinfo()
        except ImportError:
            self.stdout.write(
                'WARNING: pycdio not installed, cannot identify drive\n')
            prog.result.vendor = 'Unknown'
            prog.result.model = 'Unknown'
            prog.result.release = 'Unknown'

        prog.result.profileName = profile.name
        prog.result.profilePipeline = profile.pipeline
        elementFactory = profile.pipeline.split(' ')[0]
        prog.result.gstreamerVersion = gstreamer.gstreamerVersion()
        prog.result.gstPythonVersion = gstreamer.gstPythonVersion()
        prog.result.encoderVersion = gstreamer.elementFactoryVersion(
            elementFactory)

        # FIXME: turn this into a method

        def ripIfNotRipped(number):
            self.debug('ripIfNotRipped for track %d' % number)
            # we can have a previous result
            trackResult = prog.result.getTrackResult(number)
            if not trackResult:
                trackResult = result.TrackResult()
                prog.result.tracks.append(trackResult)
            else:
                self.debug('ripIfNotRipped have trackresult, path %r' %
                    trackResult.filename)

            path = prog.getPath(prog.outdir, self.options.track_template,
                mbdiscid, number, profile=profile) + '.' + profile.extension
            self.debug('ripIfNotRipped: path %r' % path)
            trackResult.number = number

            assert type(path) is unicode, "%r is not unicode" % path
            trackResult.filename = path
            if number > 0:
                trackResult.pregap = itable.tracks[number - 1].getPregap()

            # FIXME: optionally allow overriding reripping
            if os.path.exists(path):
                if path != trackResult.filename:
                    # the path is different (different name/template ?)
                    # but we can copy it
                    self.debug('previous result %r, expected %r' % (
                        trackResult.filename, path))

                self.stdout.write('Verifying track %d of %d: %s\n' % (
                    number, len(itable.tracks),
                    os.path.basename(path).encode('utf-8')))
                if not prog.verifyTrack(runner, trackResult):
                    self.stdout.write('Verification failed, reripping...\n')
                    os.unlink(path)

            if not os.path.exists(path):
                self.debug('path %r does not exist, ripping...' % path)
                tries = 0
                # we reset durations for test and copy here
                trackResult.testduration = 0.0
                trackResult.copyduration = 0.0
                self.stdout.write('Ripping track %d of %d: %s\n' % (
                    number, len(itable.tracks),
                    os.path.basename(path).encode('utf-8')))
                while tries < MAX_TRIES:
                    tries += 1
                    try:
                        self.debug('ripIfNotRipped: track %d, try %d',
                            number, tries)
                        prog.ripTrack(runner, trackResult,
                            offset=int(self.options.offset),
                            device=self.parentCommand.options.device,
                            profile=profile,
                            taglist=prog.getTagList(number),
                            what='track %d of %d' % (
                                number, len(itable.tracks)))
                        break
                    except Exception, e:
                        self.debug('Got exception %r on try %d',
                            e, tries)


                if tries == MAX_TRIES:
                    self.error('Giving up on track %d after %d times' % (
                        number, tries))
                if trackResult.testcrc == trackResult.copycrc:
                    self.stdout.write('Checksums match for track %d\n' %
                        number)
                else:
                    self.stdout.write(
                        'ERROR: checksums did not match for track %d\n' %
                        number)
                    raise

                self.stdout.write('Peak level: %.2f %%\n' % (
                    math.sqrt(trackResult.peak) * 100.0, ))
                self.stdout.write('Rip quality: %.2f %%\n' % (
                    trackResult.quality * 100.0, ))

            # overlay this rip onto the Table
            if number == 0:
                # HTOA goes on index 0 of track 1
                itable.setFile(1, 0, trackResult.filename,
                    ittoc.getTrackStart(1), number)
            else:
                itable.setFile(number, 1, trackResult.filename,
                    ittoc.getTrackLength(number), number)

            prog.saveRipResult()


        # check for hidden track one audio
        htoapath = None
        htoa = prog.getHTOA()
        if htoa:
            start, stop = htoa
            self.stdout.write(
                'Found Hidden Track One Audio from frame %d to %d\n' % (
                start, stop))

            # rip it
            ripIfNotRipped(0)
            htoapath = prog.result.tracks[0].filename

        for i, track in enumerate(itable.tracks):
            # FIXME: rip data tracks differently
            if not track.audio:
                self.stdout.write(
                    'WARNING: skipping data track %d, not implemented\n' % (
                    i + 1, ))
                # FIXME: make it work for now
                track.indexes[1].relative = 0
                continue

            ripIfNotRipped(i + 1)

        ### write disc files
        discName = prog.getPath(prog.outdir, self.options.disc_template,
            mbdiscid, 0, profile=profile)
        dirname = os.path.dirname(discName)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        self.debug('writing cue file for %r', discName)
        prog.writeCue(discName)

        # write .m3u file
        self.debug('writing m3u file for %r', discName)
        m3uPath = u'%s.m3u' % discName
        handle = open(m3uPath, 'w')
        handle.write(u'#EXTM3U\n')

        def writeFile(handle, path, length):
            targetPath = common.getRelativePath(path, m3uPath)
            u = u'#EXTINF:%d,%s\n' % (length, targetPath)
            handle.write(u.encode('utf-8'))
            u = '%s\n' % targetPath
            handle.write(u.encode('utf-8'))


        if htoapath:
            writeFile(handle, htoapath,
                itable.getTrackStart(1) / common.FRAMES_PER_SECOND)

        for i, track in enumerate(itable.tracks):
            if not track.audio:
                continue

            path = prog.getPath(prog.outdir, self.options.track_template,
                mbdiscid, i + 1, profile=profile) + '.' + profile.extension
            writeFile(handle, path,
                itable.getTrackLength(i + 1) / common.FRAMES_PER_SECOND)

        handle.close()

        # verify using accuraterip
        url = ittoc.getAccurateRipURL()
        self.stdout.write("AccurateRip URL %s\n" % url)

        accucache = accurip.AccuCache()
        responses = accucache.retrieve(url)

        if not responses:
            self.stdout.write('Album not found in AccurateRip database\n')

        if responses:
            self.stdout.write('%d AccurateRip reponses found\n' %
                len(responses))

            if responses[0].cddbDiscId != itable.getCDDBDiscId():
                self.stdout.write(
                    "AccurateRip response discid different: %s\n" %
                    responses[0].cddbDiscId)


        prog.verifyImage(runner, responses)

        self.stdout.write("\n".join(prog.getAccurateRipResults()) + "\n")

        prog.saveRipResult()

        # write log file
        try:
            klazz = result.getLoggers()[self.options.logger]
            prog.writeLog(discName, klazz())
        except KeyError:
            self.stderr.write("No logger named %s found!\n" % (
                self.options.logger))

        prog.ejectDevice(device)


class CD(logcommand.LogCommand):

    summary = "handle CD's"

    subCommandClasses = [Rip, ]

    def addOptions(self):
        self.parser.add_option('-d', '--device',
            action="store", dest="device",
            help="CD-DA device")

    def handleOptions(self, options):
        if not options.device:
            drives = drive.getAllDevicePaths()
            if not drives:
                self.error('No CD-DA drives found!')
                return 3

            # pick the first
            self.options.device = drives[0]

        # this can be a symlink to another device
        self.options.device = os.path.realpath(self.options.device)
