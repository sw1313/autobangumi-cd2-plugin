import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cd2.client import is_duplicate_offline_error, normalize_info_hash
from cd2.config import migrate_cd2_config
from cd2.fallback import (
    CD2FallbackManager,
    is_complete_torrent,
    is_dead_torrent,
)
from cd2.sync import (
    completed_copy_task_keys,
    find_video_in_src,
    is_local_download_complete,
    is_local_fs_path,
    local_content_size,
    move_content_to_qb,
    resolve_cd2_content,
    resolve_cd2_local_dest,
    resolve_local_base,
    resolve_offline_target,
    resolve_sync_local_base,
)


def _torrent(**kwargs):
    base = {
        "hash": "abc123",
        "name": "test",
        "progress": 0.1,
        "state": "downloading",
        "dlspeed": 0,
        "added_on": time.time() - 7200,
        "last_activity": time.time() - 7200,
        "tags": "",
    }
    base.update(kwargs)
    return base


class TestIsDeadTorrent:
    def test_completed_not_dead(self):
        assert is_dead_torrent(_torrent(progress=1.0), 3600) is False

    def test_stalled_dl_after_threshold(self):
        assert is_dead_torrent(_torrent(state="stalledDL"), 3600) is True

    def test_zero_speed_downloading_after_threshold(self):
        assert (
            is_dead_torrent(_torrent(state="downloading", dlspeed=0), 3600) is True
        )

    def test_recent_activity_not_dead(self):
        assert (
            is_dead_torrent(
                _torrent(
                    state="stalledDL",
                    last_activity=time.time() - 60,
                ),
                3600,
            )
            is False
        )

    def test_slow_speed_after_age_threshold(self):
        assert (
            is_dead_torrent(
                _torrent(
                    state="downloading",
                    dlspeed=50,
                    last_activity=time.time() - 30,
                    added_on=time.time() - 7200,
                ),
                3600,
                1024,
            )
            is True
        )

    def test_slow_speed_recent_not_dead(self):
        assert (
            is_dead_torrent(
                _torrent(
                    state="downloading",
                    dlspeed=50,
                    last_activity=time.time() - 30,
                    added_on=time.time() - 600,
                ),
                3600,
                1024,
            )
            is False
        )

    def test_fast_speed_not_dead(self):
        assert (
            is_dead_torrent(
                _torrent(
                    state="downloading",
                    dlspeed=200 * 1024,
                    added_on=time.time() - 7200,
                ),
                3600,
                1024,
            )
            is False
        )

    def test_speed_check_disabled(self):
        assert (
            is_dead_torrent(
                _torrent(
                    state="downloading",
                    dlspeed=50,
                    last_activity=time.time() - 30,
                    added_on=time.time() - 7200,
                ),
                3600,
                0,
            )
            is False
        )


class TestIsCompleteTorrent:
    def test_complete_at_100_percent(self):
        assert is_complete_torrent(_torrent(progress=1.0)) is True

    def test_incomplete_below_100_percent(self):
        assert is_complete_torrent(_torrent(progress=0.5)) is False


class TestDuplicateOfflineError:
    def test_detect_115_duplicate_code(self):
        assert is_duplicate_offline_error("code: 10008, message: 任务已存在") is True

    def test_ignore_unrelated_error(self):
        assert is_duplicate_offline_error("connection refused") is False


class TestNormalizeInfoHash:
    def test_lowercase_and_strip_prefix(self):
        assert normalize_info_hash("0xAbC123") == "abc123"


class TestResolveCd2Content:
    def test_find_by_torrent_name(self, tmp_path):
        folder = tmp_path / "Anime Title"
        folder.mkdir()
        (folder / "ep.mkv").write_bytes(b"x")
        found = resolve_cd2_content(tmp_path, "Anime Title", "deadbeef")
        assert found == folder

    def test_find_by_exact_hash_dir(self, tmp_path):
        folder = tmp_path / "deadbeef"
        folder.mkdir()
        found = resolve_cd2_content(tmp_path, "Other", "deadbeef")
        assert found == folder

    def test_does_not_search_similar_names(self, tmp_path):
        decoy = tmp_path / "task-deadbeef-files"
        decoy.mkdir()
        similar = tmp_path / "Show 2 [03].mp4"
        similar.mkdir()
        (similar / "Show 2 [03].mp4").write_bytes(b"x" * 100)
        found = resolve_cd2_content(tmp_path, "Show [03].mp4", "deadbeef")
        assert found is None

    def test_find_cd2_wrapper_folder_by_torrent_name(self, tmp_path):
        release = (
            "Haibaras.Teenage.New.Game.S01E01.The.Gray.Boys.Plan."
            "For.a.Colorful.Adolescence.1080p.CR.WEB-DL.AAC2.0.H.264-VARYG.mkv"
        )
        wrapper = tmp_path / release
        wrapper.mkdir()
        (wrapper / release).write_bytes(b"complete")
        found = resolve_cd2_content(tmp_path, release, "deadbeef")
        assert found == wrapper

    def test_does_not_steal_another_episode_wrapper(self, tmp_path):
        other = tmp_path / "Show [11].mp4"
        other.mkdir()
        (other / "Show [11].mp4").write_bytes(b"x" * 100)
        found = resolve_cd2_content(tmp_path, "Show [03].mp4", "abc123def456")
        assert found is None


class TestCompletedCopyTaskKeys:
    def test_only_completed_tasks_for_the_staging_folder(self):
        tasks = [
            SimpleNamespace(
                status=3,
                sourcePath="/115/anime/done.mp4",
                destPath="/volume1/videos/cd2-offline",
            ),
            SimpleNamespace(
                status=2,
                sourcePath="/115/anime/running.mp4",
                destPath="/volume1/videos/cd2-offline",
            ),
            SimpleNamespace(
                status=3,
                sourcePath="/115/anime/other.mp4",
                destPath="/volume1/videos/anime",
            ),
        ]
        assert completed_copy_task_keys(tasks, "/volume1/videos/cd2-offline") == [
            "/115/anime/done.mp4:/volume1/videos/cd2-offline"
        ]


class TestFindVideoInSrc:
    def test_single_video_in_wrapper_folder(self, tmp_path):
        release = "Show.S01E01.1080p.mkv"
        wrapper = tmp_path / release
        wrapper.mkdir()
        video = wrapper / release
        video.write_bytes(b"video")
        assert find_video_in_src(wrapper, release) == video

    def test_pick_largest_when_multiple_videos(self, tmp_path):
        wrapper = tmp_path / "bundle.mkv"
        wrapper.mkdir()
        small = wrapper / "sample.mkv"
        main = wrapper / "Show.S01E01.1080p.mkv"
        small.write_bytes(b"x")
        main.write_bytes(b"x" * 1000)
        assert find_video_in_src(wrapper, "Show.S01E01.1080p.mkv") == main

    def test_does_not_substitute_a_different_video(self, tmp_path):
        wrapper = tmp_path / "Ken 2 [07].mp4"
        wrapper.mkdir()
        video = wrapper / "Ken 2 [07].mp4"
        video.write_bytes(b"x" * 100)
        assert find_video_in_src(wrapper, "Haibara [02].mp4") is None


class TestMoveContentToQb:
    def test_move_directory_tree(self, tmp_path):
        src_root = tmp_path / "src"
        src = src_root / "Season 1"
        src.mkdir(parents=True)
        (src / "01.mkv").write_bytes(b"a")
        dst = tmp_path / "dst"
        move_content_to_qb(src_root, dst)
        assert (dst / "Season 1" / "01.mkv").read_bytes() == b"a"
        assert not src_root.exists()

    def test_move_single_file_to_content_path(self, tmp_path):
        src = tmp_path / "src.mkv"
        src.write_bytes(b"video")
        dst = tmp_path / "qb" / "src.mkv"
        move_content_to_qb(src, dst)
        assert dst.read_bytes() == b"video"
        assert not src.exists()

    def test_move_replaces_partial_file(self, tmp_path):
        src = tmp_path / "src.mkv"
        src.write_bytes(b"complete")
        dst = tmp_path / "qb" / "src.mkv"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"partial")
        move_content_to_qb(src, dst)
        assert dst.read_bytes() == b"complete"
        assert not src.exists()

    def test_move_from_cd2_wrapper_folder(self, tmp_path):
        release = "Show.S01E01.1080p.mkv"
        wrapper = tmp_path / release
        wrapper.mkdir()
        (wrapper / release).write_bytes(b"complete")
        dst = tmp_path / "qb" / release
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"partial")
        move_content_to_qb(wrapper, dst)
        assert dst.read_bytes() == b"complete"
        assert not wrapper.exists()

    def test_does_not_replace_a_different_release(self, tmp_path):
        wrapper = tmp_path / "Ken 2 [07].mp4"
        wrapper.mkdir()
        (wrapper / "Ken 2 [07].mp4").write_bytes(b"sakurato")
        dst = tmp_path / "qb" / "Haibara [02].mp4"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"haibara")
        with pytest.raises(FileNotFoundError):
            move_content_to_qb(wrapper, dst)
        assert dst.read_bytes() == b"haibara"
        assert wrapper.exists()


class TestResolveLocalBase:
    def test_explicit_local_path(self):
        assert resolve_local_base("/volume1/x", "/custom") == Path("/custom")

    def test_auto_map_cd2_offline(self):
        assert resolve_local_base("/volume1/videos/cd2-offline") == Path(
            "/cd2-offline"
        )

    def test_legacy_anime_cd2_offline_maps_to_cd2_offline(self):
        assert resolve_local_base("/volume1/videos/anime/cd2-offline") == Path(
            "/cd2-offline"
        )

    def test_auto_map_volume1_anime(self):
        assert resolve_local_base("/volume1/videos/anime") == Path("/anime")

    def test_container_path_passthrough(self):
        assert resolve_local_base("/cd2-offline") == Path("/cd2-offline")
        assert resolve_local_base("/anime/cd2-offline") == Path("/anime/cd2-offline")

    def test_unmapped_returns_none(self):
        assert resolve_local_base("/115/Anime") is None


class TestCd2PathHelpers:
    def test_is_local_fs_path(self):
        assert is_local_fs_path("/volume1/videos/cd2-offline") is True
        assert is_local_fs_path("/cd2-offline") is True
        assert is_local_fs_path("/volume1/videos/anime/cd2-offline") is True
        assert is_local_fs_path("/anime/cd2-offline") is True
        assert is_local_fs_path("/115/Anime/cd2-offline") is False

    def test_resolve_offline_target(self):
        assert (
            resolve_offline_target("/115/Anime/offline", "/volume1/x")
            == "/115/Anime/offline"
        )
        assert resolve_offline_target("", "/volume1/videos/anime") is None

    def test_resolve_sync_local_base(self):
        assert resolve_sync_local_base("/cd2-offline", "") == Path("/cd2-offline")
        assert resolve_sync_local_base(
            "", "/volume1/videos/cd2-offline"
        ) == Path("/cd2-offline")

    def test_resolve_cd2_local_dest(self):
        assert resolve_cd2_local_dest("/cd2-offline", "") == (
            "/volume1/videos/cd2-offline"
        )
        assert resolve_cd2_local_dest("/anime", "") == "/volume1/videos/anime"
        assert resolve_cd2_local_dest("/anime/offline", "") == (
            "/volume1/videos/anime/offline"
        )
        assert resolve_cd2_local_dest("", "/volume1/videos/anime") == (
            "/volume1/videos/anime"
        )

    def test_migrate_cd2_config_moves_local_target(self):
        migrated = migrate_cd2_config(
            {
                "target_dir": "/volume1/videos/anime/cd2-offline",
                "offline_dir": "",
                "local_path": "",
            }
        )
        assert migrated["local_path"] == "/cd2-offline"
        assert migrated["target_dir"] == ""
        assert migrated["offline_dir"] == ""

    def test_migrate_cd2_config_moves_legacy_local_path(self):
        migrated = migrate_cd2_config(
            {
                "target_dir": "",
                "offline_dir": "/115/Anime/offline",
                "local_path": "/anime/cd2-offline",
            }
        )
        assert migrated["local_path"] == "/cd2-offline"

    def test_migrate_cd2_config_keeps_cloud_target(self):
        migrated = migrate_cd2_config(
            {
                "target_dir": "/115/Anime/offline",
                "offline_dir": "",
                "local_path": "",
            }
        )
        assert migrated["offline_dir"] == "/115/Anime/offline"


class TestNeedsRedownload:
    def test_qb_content_complete_for_single_file(self, tmp_path):
        video = tmp_path / "episode.mkv"
        video.write_bytes(b"x" * 1000)
        torrent = _torrent(
            content_path=str(video),
            total_size=1000,
        )
        assert CD2FallbackManager()._qb_content_complete(torrent) is True

    def test_qb_content_missing_when_path_gone(self, tmp_path):
        video = tmp_path / "episode.mkv"
        torrent = _torrent(
            content_path=str(video),
            total_size=1000,
        )
        assert CD2FallbackManager()._qb_content_complete(torrent) is False

    @pytest.mark.asyncio
    async def test_skip_redownload_when_cloud_already_has_content(self, monkeypatch):
        manager = CD2FallbackManager()
        torrent = _torrent(name="dead torrent")

        class FakeSession:
            async def cloud_content_exists(self, paths):
                return True

        manager._session = FakeSession()

        class Cfg:
            offline_dir = "/115/anime"
            target_dir = ""
            local_path = "/cd2-offline"

        assert await manager._needs_redownload(torrent, Cfg()) is False

    @pytest.mark.asyncio
    async def test_cloud_candidates_include_cd2_wrapper_inner_file(self):
        manager = CD2FallbackManager()
        torrent = _torrent(name="release.mkv")
        checked = []

        class FakeSession:
            async def cloud_content_exists(self, paths):
                checked.extend(paths)
                return paths == ["/115/anime/release.mkv/release.mkv"]

        manager._session = FakeSession()

        paths = await manager._existing_cloud_paths("/115/anime", torrent)

        assert paths == ["/115/anime/release.mkv/release.mkv"]
        assert checked == [
            "/115/anime/release.mkv",
            "/115/anime/release.mkv/release.mkv",
        ]

    @pytest.mark.asyncio
    async def test_needs_redownload_when_everywhere_missing(self, monkeypatch):
        manager = CD2FallbackManager()
        torrent = _torrent(name="dead torrent")

        class FakeSession:
            async def cloud_content_exists(self, paths):
                return False

        manager._session = FakeSession()

        class Cfg:
            offline_dir = "/115/anime"
            target_dir = ""
            local_path = "/cd2-offline"

        assert await manager._needs_redownload(torrent, Cfg()) is True


class TestLocalDownloadComplete:
    def test_complete_when_size_matches(self, tmp_path):
        folder = tmp_path / "task"
        folder.mkdir()
        (folder / "video.mkv").write_bytes(b"x" * 1000)
        assert is_local_download_complete(folder, 1000) is True

    def test_incomplete_when_size_too_small(self, tmp_path):
        folder = tmp_path / "task"
        folder.mkdir()
        (folder / "video.mkv").write_bytes(b"x" * 100)
        assert is_local_download_complete(folder, 1000) is False

    def test_unknown_size_is_not_complete(self, tmp_path):
        file = tmp_path / "video.mkv"
        file.write_bytes(b"x")
        assert is_local_download_complete(file, 0) is False

    def test_different_size_is_not_complete(self, tmp_path):
        file = tmp_path / "video.mkv"
        file.write_bytes(b"x" * 1400)
        assert is_local_download_complete(file, 1000) is False

    def test_local_content_size_recursive(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.mkv").write_bytes(b"12345")
        (root / "sub").mkdir()
        (root / "sub" / "b.mkv").write_bytes(b"67890")
        assert local_content_size(root) == 10


class TestOfflineListFailure:
    def test_list_error_returns_zero_instead_of_raising(self):
        manager = CD2FallbackManager()

        class Session:
            async def list_finished_offline_by_hash(self, folder):
                raise RuntimeError("Transferred a partial file")

        manager._session = Session()
        result = asyncio.run(
            manager._ensure_local_copy(
                None,
                [],
                "/115/anime",
                "/volume1/videos/cd2-offline",
                Path("/cd2-offline"),
            )
        )
        assert result == 0
