# -*- Mode: Python; test-case-name: whipper.test.test_common_program -*-
# vi:si:et:sw=4:sts=4:ts=4

# Copyright (C) 2009, 2010, 2011 Thomas Vander Stichele

# This file is part of whipper.
#
# whipper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# whipper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with whipper.  If not, see <http://www.gnu.org/licenses/>.

"""
Common functionality and class for all programs using whipper.
"""

import musicbrainzngs
import os
import sys
import time

from whipper.common import common, mbngs, cache, path
from whipper.common import checksum
from whipper.program import cdrdao, cdparanoia
from whipper.image import image
from whipper.extern.task import task

import logging
logger = logging.getLogger(__name__)


# FIXME: should Program have a runner ?


class Program:
    """
    I maintain program state and functionality.

    @ivar metadata:
    @type metadata: L{mbngs.DiscMetadata}
    @ivar result:   the rip's result
    @type result:   L{result.RipResult}
    @type outdir:   unicode
    @type config:   L{whipper.common.config.Config}
    """

    cuePath = None
    logPath = None
    metadata = None
    outdir = None
    result = None

    _stdout = None

    def __init__(self, config, record=False, stdout=sys.stdout):
        """
        @param record: whether to record results of API calls for playback.
        """
        self._record = record
        self._cache = cache.ResultCache()
        self._stdout = stdout
        self._config = config

        d = {}

        for key, default in {
            'fat': True,
            'special': False
        }.items():
            value = None
            value = self._config.getboolean('main', 'path_filter_' + key)
            if value is None:
                value = default

            d[key] = value

        self._filter = path.PathFilter(**d)

    def setWorkingDirectory(self, workingDirectory):
        if workingDirectory:
            logger.info('Changing to working directory %s' % workingDirectory)
            os.chdir(workingDirectory)

    def getFastToc(self, runner, toc_pickle, device):
        """
        Retrieve the normal TOC table from a toc pickle or the drive.
        Also retrieves the cdrdao version

        @rtype: tuple of L{table.Table}, str
        """
        def function(r, t):
            r.run(t)

        ptoc = cache.Persister(toc_pickle or None)
        if not ptoc.object:
            from pkg_resources import parse_version as V
            version = cdrdao.getCDRDAOVersion()
            if V(version) < V('1.2.3rc2'):
                sys.stdout.write('Warning: cdrdao older than 1.2.3 has a '
                                 'pre-gap length bug.\n'
                                 'See http://sourceforge.net/tracker/?func=detail&aid=604751&group_id=2171&atid=102171\n')  # noqa: E501
            t = cdrdao.ReadTOCTask(device)
            ptoc.persist(t.table)
        toc = ptoc.object
        assert toc.hasTOC()
        return toc

    def getTable(self, runner, cddbdiscid, mbdiscid, device, offset):
        """
        Retrieve the Table either from the cache or the drive.

        @rtype: L{table.Table}
        """
        tcache = cache.TableCache()
        ptable = tcache.get(cddbdiscid, mbdiscid)
        itable = None
        tdict = {}

        # Ignore old cache, since we do not know what offset it used.
        if type(ptable.object) is dict:
            tdict = ptable.object

            if offset in tdict:
                itable = tdict[offset]

        if not itable:
            logger.debug('getTable: cddbdiscid %s, mbdiscid %s not '
                         'in cache for offset %s, reading table' % (
                             cddbdiscid, mbdiscid, offset))
            t = cdrdao.ReadTableTask(device)
            itable = t.table
            tdict[offset] = itable
            ptable.persist(tdict)
            logger.debug('getTable: read table %r' % itable)
        else:
            logger.debug('getTable: cddbdiscid %s, mbdiscid %s in cache '
                         'for offset %s' % (cddbdiscid, mbdiscid, offset))
            logger.debug('getTable: loaded table %r' % itable)

        assert itable.hasTOC()

        self.result.table = itable

        logger.debug('getTable: returning table with mb id %s' %
                     itable.getMusicBrainzDiscId())
        return itable

    def getRipResult(self, cddbdiscid):
        """
        Retrieve the persistable RipResult either from our cache (from a
        previous, possibly aborted rip), or return a new one.

        @rtype: L{result.RipResult}
        """
        assert self.result is None

        self._presult = self._cache.getRipResult(cddbdiscid)
        self.result = self._presult.object

        return self.result

    def saveRipResult(self):
        self._presult.persist()

    def getPath(self, outdir, template, mbdiscid, i, disambiguate=False):
        """
        Based on the template, get a complete path for the given track,
        minus extension.
        Also works for the disc name, using disc variables for the template.

        @param outdir:   the directory where to write the files
        @type  outdir:   unicode
        @param template: the template for writing the file
        @type  template: unicode
        @param i:        track number (0 for HTOA, or for disc)
        @type  i:        int

        @rtype: unicode
        """
        assert type(outdir) is unicode, "%r is not unicode" % outdir
        assert type(template) is unicode, "%r is not unicode" % template

        # the template is similar to grip, except for %s/%S/%r/%R
        # see #gripswitches

        # returns without extension

        v = {}

        v['t'] = '%02d' % i

        # default values
        v['A'] = 'Unknown Artist'
        v['d'] = mbdiscid  # fallback for title
        v['r'] = 'unknown'
        v['R'] = 'Unknown'
        v['B'] = ''  # barcode
        v['C'] = ''  # catalog number
        v['x'] = 'flac'
        v['X'] = v['x'].upper()
        v['y'] = '0000'

        v['a'] = v['A']
        if i == 0:
            v['n'] = 'Hidden Track One Audio'
        else:
            v['n'] = 'Unknown Track %d' % i

        if self.metadata:
            release = self.metadata.release or '0000'
            v['y'] = release[:4]
            v['A'] = self._filter.filter(self.metadata.artist)
            v['S'] = self._filter.filter(self.metadata.sortName)
            v['d'] = self._filter.filter(self.metadata.title)
            v['B'] = self.metadata.barcode
            v['C'] = self.metadata.catalogNumber
            if self.metadata.releaseType:
                v['R'] = self.metadata.releaseType
                v['r'] = self.metadata.releaseType.lower()
            if i > 0:
                try:
                    v['a'] = self._filter.filter(
                        self.metadata.tracks[i - 1].artist)
                    v['s'] = self._filter.filter(
                        self.metadata.tracks[i - 1].sortName)
                    v['n'] = self._filter.filter(
                        self.metadata.tracks[i - 1].title)
                except IndexError, e:
                    print 'ERROR: no track %d found, %r' % (i, e)
                    raise
            else:
                # htoa defaults to disc's artist
                v['a'] = self._filter.filter(self.metadata.artist)

        # when disambiguating, use catalogNumber then barcode
        if disambiguate:
            templateParts = list(os.path.split(template))
            if self.metadata.catalogNumber:
                templateParts[-2] += ' (%s)' % self.metadata.catalogNumber
            elif self.metadata.barcode:
                templateParts[-2] += ' (%s)' % self.metadata.barcode
            template = os.path.join(*templateParts)
            logger.debug('Disambiguated template to %r' % template)

        import re
        template = re.sub(r'%(\w)', r'%(\1)s', template)

        ret = os.path.join(outdir, template % v)

        return ret

    def getCDDB(self, cddbdiscid):
        """
        @param cddbdiscid: list of id, tracks, offsets, seconds

        @rtype: str
        """
        # FIXME: convert to nonblocking?
        import CDDB
        try:
            code, md = CDDB.query(cddbdiscid)
            logger.debug('CDDB query result: %r, %r', code, md)
            if code == 200:
                return md['title']

        except IOError, e:
            # FIXME: for some reason errno is a str ?
            if e.errno == 'socket error':
                self._stdout.write("Warning: network error: %r\n" % (e, ))
            else:
                raise

        return None

    def getMusicBrainz(self, ittoc, mbdiscid, release=None, country=None,
                       prompt=False):
        """
        @type  ittoc: L{whipper.image.table.Table}
        """
        # look up disc on MusicBrainz
        self._stdout.write('Disc duration: %s, %d audio tracks\n' % (
            common.formatTime(ittoc.duration() / 1000.0),
            ittoc.getAudioTracks()))
        logger.debug('MusicBrainz submit url: %r',
                     ittoc.getMusicBrainzSubmitURL())
        ret = None

        metadatas = None
        e = None

        for _ in range(0, 4):
            try:
                metadatas = mbngs.musicbrainz(mbdiscid,
                                              country=country,
                                              record=self._record)
                break
            except mbngs.NotFoundException, e:
                break
            except musicbrainzngs.NetworkError, e:
                self._stdout.write("Warning: network error: %r\n" % (e, ))
                break
            except mbngs.MusicBrainzException, e:
                self._stdout.write("Warning: %r\n" % (e, ))
                time.sleep(5)
                continue

        if not metadatas:
            if e:
                self._stdout.write("Error: %r\n" % (e, ))
            self._stdout.write('Continuing without metadata\n')

        if metadatas:
            deltas = {}

            self._stdout.write('\nMatching releases:\n')

            for metadata in metadatas:
                self._stdout.write('\n')
                self._stdout.write('Artist  : %s\n' %
                                   metadata.artist.encode('utf-8'))
                self._stdout.write('Title   : %s\n' %
                                   metadata.title.encode('utf-8'))
                self._stdout.write('Duration: %s\n' %
                                   common.formatTime(metadata.duration /
                                                     1000.0))
                self._stdout.write('URL     : %s\n' % metadata.url)
                self._stdout.write('Release : %s\n' % metadata.mbid)
                self._stdout.write('Type    : %s\n' % metadata.releaseType)
                if metadata.barcode:
                    self._stdout.write("Barcode : %s\n" % metadata.barcode)
                if metadata.catalogNumber:
                    self._stdout.write("Cat no  : %s\n" %
                                       metadata.catalogNumber)
                if metadata.mediumFormat:
                    self._stdout.write("Format  : %s\n" %
                                       metadata.mediumFormat)

                delta = abs(metadata.duration - ittoc.duration())
                if delta not in deltas:
                    deltas[delta] = []
                deltas[delta].append(metadata)

            lowest = None

            if not release and len(metadatas) > 1:
                # Select the release that most closely matches the duration.
                lowest = min(deltas.keys())

                if prompt:
                    guess = (deltas[lowest])[0].mbid
                    release = raw_input(
                        "\nPlease select a release [%s]: " % guess)

                    if not release:
                        release = guess

            if release:
                metadatas = [m for m in metadatas if m.url.endswith(release)]
                logger.debug('Asked for release %r, only kept %r',
                             release, metadatas)
                if len(metadatas) == 1:
                    self._stdout.write('\n')
                    self._stdout.write('Picked requested release id %s\n' %
                                       release)
                    self._stdout.write('Artist : %s\n' %
                                       metadatas[0].artist.encode('utf-8'))
                    self._stdout.write('Title :  %s\n' %
                                       metadatas[0].title.encode('utf-8'))
                elif not metadatas:
                    self._stdout.write(
                        "Requested release id '%s', "
                        "but none of the found releases match\n" % release)
                    return
            else:
                if lowest:
                    metadatas = deltas[lowest]

            # If we have multiple, make sure they match
            if len(metadatas) > 1:
                artist = metadatas[0].artist
                releaseTitle = metadatas[0].releaseTitle
                for i, metadata in enumerate(metadatas):
                    if not artist == metadata.artist:
                        logger.warning("artist 0: %r and artist %d: %r "
                                       "are not the same" % (
                                           artist, i, metadata.artist))
                    if not releaseTitle == metadata.releaseTitle:
                        logger.warning("title 0: %r and title %d: %r "
                                       "are not the same" % (
                                           releaseTitle, i,
                                           metadata.releaseTitle))

                if (not release and len(deltas.keys()) > 1):
                    self._stdout.write('\n')
                    self._stdout.write('Picked closest match in duration.\n')
                    self._stdout.write('Others may be wrong in MusicBrainz, '
                                       'please correct.\n')
                    self._stdout.write('Artist : %s\n' %
                                       artist.encode('utf-8'))
                    self._stdout.write('Title :  %s\n' %
                                       metadatas[0].title.encode('utf-8'))

            # Select one of the returned releases. We just pick the first one.
            ret = metadatas[0]
        else:
            self._stdout.write(
                'Submit this disc to MusicBrainz at the above URL.\n')
            ret = None

        self._stdout.write('\n')
        return ret

    def getTagList(self, number):
        """
        Based on the metadata, get a dict of tags for the given track.

        @param number:   track number (0 for HTOA)
        @type  number:   int

        @rtype: dict
        """
        trackArtist = u'Unknown Artist'
        albumArtist = u'Unknown Artist'
        disc = u'Unknown Disc'
        title = u'Unknown Track'

        if self.metadata:
            trackArtist = self.metadata.artist
            albumArtist = self.metadata.artist
            disc = self.metadata.title
            mbidAlbum = self.metadata.mbid
            mbidTrackAlbum = self.metadata.mbidArtist
            mbDiscId = self.metadata.discid

            if number > 0:
                try:
                    track = self.metadata.tracks[number - 1]
                    trackArtist = track.artist
                    title = track.title
                    mbidTrack = track.mbid
                    mbidTrackArtist = track.mbidArtist
                except IndexError, e:
                    print 'ERROR: no track %d found, %r' % (number, e)
                    raise
            else:
                # htoa defaults to disc's artist
                title = 'Hidden Track One Audio'

        tags = {}

        if self.metadata and not self.metadata.various:
            tags['ALBUMARTIST'] = albumArtist
        tags['ARTIST'] = trackArtist
        tags['TITLE'] = title
        tags['ALBUM'] = disc

        tags['TRACKNUMBER'] = u'%s' % number

        if self.metadata:
            if self.metadata.release is not None:
                tags['DATE'] = self.metadata.release

            if number > 0:
                tags['MUSICBRAINZ_TRACKID'] = mbidTrack
                tags['MUSICBRAINZ_ARTISTID'] = mbidTrackArtist
                tags['MUSICBRAINZ_ALBUMID'] = mbidAlbum
                tags['MUSICBRAINZ_ALBUMARTISTID'] = mbidTrackAlbum
                tags['MUSICBRAINZ_DISCID'] = mbDiscId

        # TODO/FIXME: ISRC tag

        return tags

    def getHTOA(self):
        """
        Check if we have hidden track one audio.

        @returns: tuple of (start, stop), or None
        """
        track = self.result.table.tracks[0]
        try:
            index = track.getIndex(0)
        except KeyError:
            return None

        start = index.absolute
        stop = track.getIndex(1).absolute - 1
        return (start, stop)

    def verifyTrack(self, runner, trackResult):

        t = checksum.CRC32Task(trackResult.filename)

        try:
            runner.run(t)
        except task.TaskException, e:
            if isinstance(e.exception, common.MissingFrames):
                logger.warning('missing frames for %r' % trackResult.filename)
                return False
            else:
                raise

        ret = trackResult.testcrc == t.checksum
        logger.debug('verifyTrack: track result crc %r, '
                     'file crc %r, result %r',
                     trackResult.testcrc, t.checksum, ret)
        return ret

    def ripTrack(self, runner, trackResult, offset, device, taglist,
                 overread, what=None):
        """
        Ripping the track may change the track's filename as stored in
        trackResult.

        @param trackResult: the object to store information in.
        @type  trackResult: L{result.TrackResult}
        """
        if trackResult.number == 0:
            start, stop = self.getHTOA()
        else:
            start = self.result.table.getTrackStart(trackResult.number)
            stop = self.result.table.getTrackEnd(trackResult.number)

        dirname = os.path.dirname(trackResult.filename)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        if not what:
            what = 'track %d' % (trackResult.number, )

        t = cdparanoia.ReadVerifyTrackTask(trackResult.filename,
                                           self.result.table, start,
                                           stop, overread,
                                           offset=offset,
                                           device=device,
                                           taglist=taglist,
                                           what=what)

        runner.run(t)

        logger.debug('ripped track')
        logger.debug('test speed %.3f/%.3f seconds' % (
            t.testspeed, t.testduration))
        logger.debug('copy speed %.3f/%.3f seconds' % (
            t.copyspeed, t.copyduration))
        trackResult.testcrc = t.testchecksum
        trackResult.copycrc = t.copychecksum
        trackResult.peak = t.peak
        trackResult.quality = t.quality
        trackResult.testspeed = t.testspeed
        trackResult.copyspeed = t.copyspeed
        # we want rerips to add cumulatively to the time
        trackResult.testduration += t.testduration
        trackResult.copyduration += t.copyduration

        if trackResult.filename != t.path:
            trackResult.filename = t.path
            logger.info('Filename changed to %r', trackResult.filename)

    def retagImage(self, runner, taglists):
        cueImage = image.Image(self.cuePath)
        t = image.ImageRetagTask(cueImage, taglists)
        runner.run(t)

    def verifyImage(self, runner, responses):
        """
        Verify our image against the given AccurateRip responses.

        Needs an initialized self.result.
        Will set accurip and friends on each TrackResult.
        """

        logger.debug('verifying Image against %d AccurateRip responses',
                     len(responses or []))

        cueImage = image.Image(self.cuePath)
        verifytask = image.ImageVerifyTask(cueImage)
        cuetask = image.AccurateRipChecksumTask(cueImage)
        runner.run(verifytask)
        runner.run(cuetask)

        self._verifyImageWithChecksums(responses, cuetask.checksums)

    def _verifyImageWithChecksums(self, responses, checksums):
        # loop over tracks to set our calculated AccurateRip CRC's
        for i, csum in enumerate(checksums):
            trackResult = self.result.getTrackResult(i + 1)
            trackResult.ARCRC = csum

        if not responses:
            logger.warning('No AccurateRip responses, cannot verify.')
            return

        # now loop to match responses
        for i, csum in enumerate(checksums):
            trackResult = self.result.getTrackResult(i + 1)

            confidence = None
            response = None

            # match against each response's checksum for this track
            for j, r in enumerate(responses):
                if "%08x" % csum == r.checksums[i]:
                    response = r
                    logger.debug(
                        "Track %02d matched response %d of %d in "
                        "AccurateRip database",
                        i + 1, j + 1, len(responses))
                    trackResult.accurip = True
                    # FIXME: maybe checksums should be ints
                    trackResult.ARDBCRC = int(r.checksums[i], 16)
                    # arsum = csum
                    confidence = r.confidences[i]
                    trackResult.ARDBConfidence = confidence

            if not trackResult.accurip:
                logger.warning("Track %02d: not matched in "
                               "AccurateRip database", i + 1)

            # I have seen AccurateRip responses with 0 as confidence
            # for example, Best of Luke Haines, disc 1, track 1
            maxConfidence = -1
            maxResponse = None
            for r in responses:
                if r.confidences[i] > maxConfidence:
                    maxConfidence = r.confidences[i]
                    maxResponse = r

            logger.debug('Track %02d: found max confidence %d' % (
                i + 1, maxConfidence))
            trackResult.ARDBMaxConfidence = maxConfidence
            if not response:
                logger.warning('Track %02d: none of the responses matched.',
                               i + 1)
                trackResult.ARDBCRC = int(
                    maxResponse.checksums[i], 16)
            else:
                trackResult.ARDBCRC = int(response.checksums[i], 16)

    # TODO MW: Update this further for ARv2 code
    def getAccurateRipResults(self):
        """
        @rtype: list of str
        """
        res = []

        # loop over tracks
        for i, trackResult in enumerate(self.result.tracks):
            status = 'rip NOT accurate'

            if trackResult.accurip:
                status = 'rip accurate    '

            c = "(not found)            "
            ar = ", DB [notfound]"
            if trackResult.ARDBMaxConfidence:
                c = "(max confidence    %3d)" % trackResult.ARDBMaxConfidence
                if trackResult.ARDBConfidence is not None:
                    if trackResult.ARDBConfidence \
                            < trackResult.ARDBMaxConfidence:
                        c = "(confidence %3d of %3d)" % (
                            trackResult.ARDBConfidence,
                            trackResult.ARDBMaxConfidence)

                ar = ", DB [%08x]" % trackResult.ARDBCRC
            # htoa tracks (i == 0) do not have an ARCRC
            if trackResult.ARCRC is None:
                assert trackResult.number == 0, \
                    'no trackResult.ARCRC on non-HTOA track %d' % \
                    trackResult.number
                res.append("Track  0: unknown          (not tracked)")
            else:
                res.append("Track %2d: %s %s [%08x]%s" % (
                    trackResult.number, status, c, trackResult.ARCRC, ar))

        return res

    def writeCue(self, discName):
        assert self.result.table.canCue()
        cuePath = '%s.cue' % discName
        logger.debug('write .cue file to %s', cuePath)
        handle = open(cuePath, 'w')
        # FIXME: do we always want utf-8 ?
        handle.write(self.result.table.cue(cuePath).encode('utf-8'))
        handle.close()

        self.cuePath = cuePath

        return cuePath

    def writeLog(self, discName, logger):
        logPath = '%s.log' % discName
        handle = open(logPath, 'w')
        log = logger.log(self.result)
        handle.write(log.encode('utf-8'))
        handle.close()

        self.logPath = logPath

        return logPath
