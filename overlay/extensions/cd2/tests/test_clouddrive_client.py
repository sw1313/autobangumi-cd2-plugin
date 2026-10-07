import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import grpc
import pytest

from cd2.client import (
    CD2Client,
    _file_has_content,
    iter_sub_files,
)


def _sub_files_reply(*files):
    return SimpleNamespace(subFiles=list(files))


class TestFileHasContent:
    def test_directory_counts_as_content(self):
        assert _file_has_content(SimpleNamespace(isDirectory=True, size=0)) is True

    def test_nonzero_file_counts_as_content(self):
        assert _file_has_content(SimpleNamespace(isDirectory=False, size=1)) is True

    def test_empty_file_is_not_content(self):
        assert _file_has_content(SimpleNamespace(isDirectory=False, size=0)) is False


class TestIterSubFiles:
    def test_yields_incrementally_and_stops_early(self):
        client = MagicMock()
        client._create_authorized_metadata.return_value = [("authorization", "Bearer x")]

        call = MagicMock()
        call.__iter__.return_value = iter(
            [
                _sub_files_reply(SimpleNamespace(name="a", size=0)),
                _sub_files_reply(SimpleNamespace(name="b", size=1)),
            ]
        )
        client.stub.GetSubFiles.return_value = call

        seen = []
        for item in iter_sub_files(client, "/115/anime/"):
            seen.append(item)
            if getattr(item, "size", 0):
                break

        assert [item.name for item in seen] == ["a", "b"]
        client.stub.GetSubFiles.assert_called_once()

    def test_honours_cancel_event(self):
        client = MagicMock()
        client._create_authorized_metadata.return_value = []

        call = MagicMock()
        call.__iter__.return_value = iter(
            [
                _sub_files_reply(SimpleNamespace(name="a", size=0)),
                _sub_files_reply(SimpleNamespace(name="b", size=0)),
            ]
        )
        client.stub.GetSubFiles.return_value = call

        cancel = threading.Event()
        cancel.set()
        items = list(iter_sub_files(client, "/115/anime/", cancel_event=cancel))

        assert items == []
        call.cancel.assert_called_once()

    def test_swallows_rpc_error_after_cancel(self):
        client = MagicMock()
        client._create_authorized_metadata.return_value = []

        call = MagicMock()
        call.__iter__.side_effect = grpc.RpcError()
        client.stub.GetSubFiles.return_value = call

        cancel = threading.Event()
        cancel.set()
        assert list(iter_sub_files(client, "/115/anime/", cancel_event=cancel)) == []


class TestPathHasContent:
    def test_directory_returns_true_on_first_nonempty_child(self, monkeypatch):
        cd2 = CD2Client("127.0.0.1:19798", "u", "p")
        cd2._client = MagicMock()

        def fake_iter(_client, path, **_kwargs):
            if path == "/115/anime/":
                return iter(
                    [
                        SimpleNamespace(
                            name="show",
                            fullPathName="/115/anime/show",
                            isDirectory=True,
                            size=0,
                        )
                    ]
                )
            return iter(
                [
                    SimpleNamespace(isDirectory=False, size=0),
                    SimpleNamespace(isDirectory=False, size=1024),
                    SimpleNamespace(isDirectory=False, size=2048),
                ]
            )

        monkeypatch.setattr("cd2.client.iter_sub_files", fake_iter)
        assert cd2.path_has_content("/115/anime/show") is True

    def test_returns_false_when_parent_listing_has_no_exact_child(self, monkeypatch):
        cd2 = CD2Client("127.0.0.1:19798", "u", "p")
        cd2._client = MagicMock()

        monkeypatch.setattr(
            "cd2.client.iter_sub_files",
            lambda *_args, **_kwargs: iter(
                [
                    SimpleNamespace(
                        name="other-file.mkv",
                        fullPathName="/115/anime/other-file.mkv",
                        isDirectory=False,
                        size=1024,
                    )
                ]
            ),
        )

        assert cd2.path_has_content("/115/anime/missing-file.mkv") is False

    def test_nested_cd2_offline_file_path_matches_inner_file(self, monkeypatch):
        cd2 = CD2Client("127.0.0.1:19798", "u", "p")
        cd2._client = MagicMock()

        def fake_iter(_client, path, **_kwargs):
            if path == "/115/anime/release.mkv/":
                return iter(
                    [
                        SimpleNamespace(
                            name="release.mkv",
                            fullPathName="/115/anime/release.mkv/release.mkv",
                            isDirectory=False,
                            size=1024,
                        )
                    ]
                )
            return iter([])

        monkeypatch.setattr("cd2.client.iter_sub_files", fake_iter)

        assert (
            cd2.path_has_content("/115/anime/release.mkv/release.mkv")
            is True
        )
