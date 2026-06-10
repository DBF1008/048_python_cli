import os
import tempfile
import time
import requests
from unittest import mock
from urllib.request import urlopen

import pytest
from requests.structures import CaseInsensitiveDict

from httpie.downloads import (
    parse_content_range, filename_from_content_disposition, filename_from_url,
    get_unique_filename, trim_filename, ContentRangeError, Downloader, PARTIAL_CONTENT
)
from .utils import http, MockEnvironment


class Response(requests.Response):
    # noinspection PyDefaultArgument
    def __init__(self, url, headers={}, status_code=200):
        self.url = url
        self.headers = CaseInsensitiveDict(headers)
        self.status_code = status_code


class TestDownloadUtils:

    def test_Content_Range_parsing(self):
        parse = parse_content_range

        assert parse('bytes 100-199/200', 100) == 200
        assert parse('bytes 100-199/*', 100) == 200

        # single byte
        assert parse('bytes 100-100/*', 100) == 101

        # missing
        pytest.raises(ContentRangeError, parse, None, 100)

        # syntax error
        pytest.raises(ContentRangeError, parse, 'beers 100-199/*', 100)

        # unexpected range
        pytest.raises(ContentRangeError, parse, 'bytes 100-199/*', 99)

        # invalid instance-length
        pytest.raises(ContentRangeError, parse, 'bytes 100-199/199', 100)

        # invalid byte-range-resp-spec
        pytest.raises(ContentRangeError, parse, 'bytes 100-99/199', 100)

    @pytest.mark.parametrize('header, expected_filename', [
        ('attachment; filename=hello-WORLD_123.txt', 'hello-WORLD_123.txt'),
        ('attachment; filename=".hello-WORLD_123.txt"', 'hello-WORLD_123.txt'),
        ('attachment; filename="white space.txt"', 'white space.txt'),
        (r'attachment; filename="\"quotes\".txt"', '"quotes".txt'),
        ('attachment; filename=/etc/hosts', 'hosts'),
        ('attachment; filename=', None)
    ])
    def test_Content_Disposition_parsing(self, header, expected_filename):
        assert filename_from_content_disposition(header) == expected_filename

    def test_filename_from_url(self):
        assert 'foo.txt' == filename_from_url(
            url='http://example.org/foo',
            content_type='text/plain'
        )
        assert 'foo.html' == filename_from_url(
            url='http://example.org/foo',
            content_type='text/html; charset=UTF-8'
        )
        assert 'foo' == filename_from_url(
            url='http://example.org/foo',
            content_type=None
        )
        assert 'foo' == filename_from_url(
            url='http://example.org/foo',
            content_type='x-foo/bar'
        )

    @pytest.mark.parametrize(
        'orig_name, unique_on_attempt, expected',
        [
            # Simple
            ('foo.bar', 0, 'foo.bar'),
            ('foo.bar', 1, 'foo-1.bar'),
            ('foo.bar', 10, 'foo-10.bar'),
            # Trim
            ('A' * 20, 0, 'A' * 10),
            ('A' * 20, 1, 'A' * 8 + '-1'),
            ('A' * 20, 10, 'A' * 7 + '-10'),
            # Trim before ext
            ('A' * 20 + '.txt', 0, 'A' * 6 + '.txt'),
            ('A' * 20 + '.txt', 1, 'A' * 4 + '-1' + '.txt'),
            # Trim at the end
            ('foo.' + 'A' * 20, 0, 'foo.' + 'A' * 6),
            ('foo.' + 'A' * 20, 1, 'foo.' + 'A' * 4 + '-1'),
            ('foo.' + 'A' * 20, 10, 'foo.' + 'A' * 3 + '-10'),
        ]
    )
    @mock.patch('httpie.downloads.get_filename_max_length')
    def test_unique_filename(self, get_filename_max_length,
                             orig_name, unique_on_attempt,
                             expected):

        def attempts(unique_on_attempt=0):
            # noinspection PyUnresolvedReferences,PyUnusedLocal
            def exists(filename):
                if exists.attempt == unique_on_attempt:
                    return False
                exists.attempt += 1
                return True

            exists.attempt = 0
            return exists

        get_filename_max_length.return_value = 10

        actual = get_unique_filename(orig_name, attempts(unique_on_attempt))
        assert expected == actual

    def test_Content_Range_parsing_whitespace(self):
        parse = parse_content_range
        assert parse('bytes 100-199/200 ', 100) == 200
        assert parse(' bytes 100-199/200', 100) == 200
        assert parse(' bytes 100-199/200 ', 100) == 200
        assert parse('bytes  100-199/200', 100) == 200
        assert parse('bytes   100-199/200', 100) == 200
        assert parse('bytes\t100-199/200', 100) == 200

    def test_trim_filename_edge_cases(self):
        # Short name, long extension: trim extension
        result = trim_filename('ab.' + 'A' * 22, 10)
        assert result == 'ab.' + 'A' * 7
        assert len(result) == 10

        # Extremely short max_len: drop extension entirely
        result = trim_filename('ab.AAAAAA', 1)
        assert result == 'a'
        assert len(result) == 1

        # No extension
        assert trim_filename('A' * 20, 10) == 'A' * 10
        assert trim_filename('A' * 20, 5) == 'A' * 5

        # Normal case: trim name, preserve extension
        assert trim_filename('A' * 20 + '.txt', 10) == 'A' * 6 + '.txt'

    @pytest.mark.parametrize(
        'orig_name, unique_on_attempt, expected',
        [
            ('archive.tar.gz', 1, 'archive.tar-1.gz'),
            ('archive.tar.gz', 10, 'archive.tar-10.gz'),
            ('README', 1, 'README-1'),
            ('.hidden', 1, '.hidden-1'),
        ]
    )
    @mock.patch('httpie.downloads.get_filename_max_length')
    def test_unique_filename_suffix_before_extension(
        self, get_filename_max_length,
        orig_name, unique_on_attempt, expected
    ):
        def attempts(unique_on_attempt=0):
            def exists(filename):
                if exists.attempt == unique_on_attempt:
                    return False
                exists.attempt += 1
                return True
            exists.attempt = 0
            return exists

        get_filename_max_length.return_value = 255
        actual = get_unique_filename(orig_name, attempts(unique_on_attempt))
        assert expected == actual


class TestDownloads:

    def test_actual_download(self, httpbin_both, httpbin):
        robots_txt = '/robots.txt'
        body = urlopen(httpbin + robots_txt).read().decode()
        env = MockEnvironment(stdin_isatty=True, stdout_isatty=False, show_displays=True)
        r = http('--download', httpbin_both.url + robots_txt, env=env)
        assert 'Downloading' in r.stderr
        assert body == r

    def test_download_with_Content_Length(self, mock_env, httpbin_both):
        with open(os.devnull, 'w') as devnull:
            downloader = Downloader(mock_env, output_file=devnull)
            downloader.start(
                initial_url='/',
                final_response=Response(
                    url=httpbin_both.url + '/',
                    headers={'Content-Length': 10}
                )
            )
            time.sleep(1.1)
            downloader.chunk_downloaded(b'12345')
            time.sleep(1.1)
            downloader.chunk_downloaded(b'12345')
            downloader.finish()
            assert not downloader.interrupted

    def test_download_no_Content_Length(self, mock_env, httpbin_both):
        with open(os.devnull, 'w') as devnull:
            downloader = Downloader(mock_env, output_file=devnull)
            downloader.start(
                final_response=Response(url=httpbin_both.url + '/'),
                initial_url='/'
            )
            time.sleep(1.1)
            downloader.chunk_downloaded(b'12345')
            downloader.finish()
            assert not downloader.interrupted

    def test_download_output_from_content_disposition(self, mock_env, httpbin_both):
        with tempfile.TemporaryDirectory() as tmp_dirname:
            orig_cwd = os.getcwd()
            os.chdir(tmp_dirname)
            try:
                assert not os.path.isfile('filename.bin')
                downloader = Downloader(mock_env)
                downloader.start(
                    final_response=Response(
                        url=httpbin_both.url + '/',
                        headers={
                            'Content-Length': 5,
                            'Content-Disposition': 'attachment; filename="filename.bin"',
                        }
                    ),
                    initial_url='/'
                )
                downloader.chunk_downloaded(b'12345')
                downloader.finish()
                downloader.failed()  # Stop the reporter
                assert not downloader.interrupted

                # TODO: Auto-close the file in that case?
                downloader._output_file.close()
                assert os.path.isfile('filename.bin')
            finally:
                os.chdir(orig_cwd)

    def test_download_interrupted(self, mock_env, httpbin_both):
        with open(os.devnull, 'w') as devnull:
            downloader = Downloader(mock_env, output_file=devnull)
            downloader.start(
                final_response=Response(
                    url=httpbin_both.url + '/',
                    headers={'Content-Length': 5}
                ),
                initial_url='/'
            )
            downloader.chunk_downloaded(b'1234')
            downloader.finish()
            assert downloader.interrupted

    def test_download_resumed(self, mock_env, httpbin_both):
        with tempfile.TemporaryDirectory() as tmp_dirname:
            file = os.path.join(tmp_dirname, 'file.bin')
            with open(file, 'a'):
                pass

            with open(file, 'a+b') as output_file:
                # Start and interrupt the transfer after 3 bytes written
                downloader = Downloader(mock_env, output_file=output_file)
                downloader.start(
                    final_response=Response(
                        url=httpbin_both.url + '/',
                        headers={'Content-Length': 5}
                    ),
                    initial_url='/'
                )
                downloader.chunk_downloaded(b'123')
                downloader.finish()
                downloader.failed()
                assert downloader.interrupted

            # Write bytes
            with open(file, 'wb') as fh:
                fh.write(b'123')

            with open(file, 'a+b') as output_file:
                # Resume the transfer
                downloader = Downloader(mock_env, output_file=output_file, resume=True)

                # Ensure `pre_request()` is working as expected too
                headers = {}
                downloader.pre_request(headers)
                assert headers['Accept-Encoding'] == 'identity'
                assert headers['Range'] == 'bytes=3-'

                downloader.start(
                    final_response=Response(
                        url=httpbin_both.url + '/',
                        headers={'Content-Length': 5, 'Content-Range': 'bytes 3-4/5'},
                        status_code=PARTIAL_CONTENT
                    ),
                    initial_url='/'
                )
                downloader.chunk_downloaded(b'45')
                downloader.finish()

    def test_download_with_redirect_original_url_used_for_filename(self, httpbin):
        # Redirect from `/redirect/1` to `/get`.
        expected_filename = '1.json'
        orig_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp_dirname:
            os.chdir(tmp_dirname)
            try:
                assert os.listdir('.') == []
                http('--download', httpbin + '/redirect/1')
                assert os.listdir('.') == [expected_filename]
            finally:
                os.chdir(orig_cwd)

    def test_download_resume_invalid_content_range_fallback(
        self, mock_env, httpbin_both
    ):
        """206 + invalid Content-Range should fall back, not crash."""
        with tempfile.TemporaryDirectory() as tmp_dirname:
            file = os.path.join(tmp_dirname, 'file.bin')
            with open(file, 'wb') as fh:
                fh.write(b'123')

            with open(file, 'a+b') as output_file:
                downloader = Downloader(
                    mock_env, output_file=output_file, resume=True
                )
                headers = {}
                downloader.pre_request(headers)
                assert downloader._resumed_from == 3

                downloader.start(
                    final_response=Response(
                        url=httpbin_both.url + '/',
                        headers={
                            'Content-Length': 10,
                            'Content-Range': 'invalid-range-header',
                        },
                        status_code=PARTIAL_CONTENT
                    ),
                    initial_url='/'
                )

                # Should have fallen back to fresh download
                assert downloader._resumed_from == 0
                assert downloader.status.total_size is None
                output_file.seek(0, 2)
                assert output_file.tell() == 0

                downloader.chunk_downloaded(b'0123456789')
                downloader.finish()
                downloader.failed()
                assert not downloader.interrupted

    def test_download_resume_206_no_content_range_header(
        self, mock_env, httpbin_both
    ):
        """206 + missing Content-Range header should fall back gracefully."""
        with tempfile.TemporaryDirectory() as tmp_dirname:
            file = os.path.join(tmp_dirname, 'file.bin')
            with open(file, 'wb') as fh:
                fh.write(b'partial')

            with open(file, 'a+b') as output_file:
                downloader = Downloader(
                    mock_env, output_file=output_file, resume=True
                )
                headers = {}
                downloader.pre_request(headers)

                downloader.start(
                    final_response=Response(
                        url=httpbin_both.url + '/',
                        headers={'Content-Length': 5},
                        status_code=PARTIAL_CONTENT
                    ),
                    initial_url='/'
                )

                assert downloader._resumed_from == 0
                assert downloader.status.total_size is None

                downloader.chunk_downloaded(b'12345')
                downloader.finish()
                downloader.failed()
                assert not downloader.interrupted
