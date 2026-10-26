def safe_session_key(session_id: str) -> str:
    """session_idをパストラバーサルできないファイル名へ変換する。"""
    raw = str(session_id)
    safe = SAFE_SESSION_CHARS.sub("_", raw).strip("._")[:80]
    if safe and safe == raw:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe or 'session'}-{digest}"


from contextvars import ContextVar

_SAVE_SKIP_REASON = ContextVar("obsidian_save_skip_reason", default=None)


def mark_save_skipped(reason: str) -> None:
    """入力本文やパスを記録せず、保存を見送った理由だけを残す。"""
    if _SAVE_SKIP_REASON.get() is None:
        _SAVE_SKIP_REASON.set(reason)


def diagnostic_path(session_id: str) -> Path:
    return STATE_DIR / ".diagnostics" / (safe_session_key(session_id) + ".status")


def record_save_observation(session_id: str, details: dict) -> None:
    """本文・例外メッセージを含めず、診断情報を保存する。診断失敗は保存を妨げない。"""
    try:
        path = diagnostic_path(session_id)
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        if details.get("last_outcome") != "skipped":
            previous.pop("skip_reason", None)
        previous.update(details, session_id=str(session_id), saver_version=__version__)
        atomic_write_text(path, json.dumps(previous, ensure_ascii=False, indent=2))
    except (OSError, ValueError):
        logger.warning("診断情報を保存できませんでした")


@contextmanager
def observe_save(session_id: str, payload: dict):
    """hook/bridgeの完了・例外を記録する。通常の終了コード契約は呼出元へ任せる。"""
    details = {"last_attempt_at": format_iso(now_jst()), "last_outcome": "skipped"}
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if isinstance(transcript, str) and transcript:
        details["transcript_path"] = transcript
    state_file = state_path(session_id)
    try:
        before = state_file.stat().st_mtime_ns
    except OSError:
        before = None
    class ErrorObserver(logging.Handler):
        def emit(self, record):
            details.update(last_outcome="failed", last_failure_at=format_iso(now_jst()),
                           last_failure_type="logged_error")
    observer = ErrorObserver(level=logging.ERROR)
    logger.addHandler(observer)
    skip_token = _SAVE_SKIP_REASON.set(None)
    try:
        yield
    except Exception as error:
        details.update(last_outcome="failed", last_failure_at=format_iso(now_jst()),
                       last_failure_type=type(error).__name__)
        raise
    else:
        state = load_state(session_id)
        output = state.get("output_path")
        try:
            after = state_file.stat().st_mtime_ns
        except OSError:
            after = None
        if details["last_outcome"] == "skipped" and _SAVE_SKIP_REASON.get() is not None:
            details["skip_reason"] = _SAVE_SKIP_REASON.get()
        elif (details["last_outcome"] != "failed" and isinstance(output, str)
                and Path(output).is_file() and after != before):
            details.update(last_outcome="saved", last_success_at=format_iso(now_jst()),
                           output_path=str(output))
        elif details["last_outcome"] == "skipped":
            details["skip_reason"] = _SAVE_SKIP_REASON.get() or "unchanged"
    finally:
        _SAVE_SKIP_REASON.reset(skip_token)
        logger.removeHandler(observer)
        record_save_observation(session_id, details)


def state_path(session_id: str) -> Path:
    return STATE_DIR / f"{safe_session_key(session_id)}.json"


def atomic_write_text(path: Path, content: str) -> None:
    """同じディレクトリの一時ファイルをrenameして文字列を保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        if path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o7777)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


class ExistingMarkdownSearchError(Exception):
    """既存Markdown探索中のI/Oエラー。誤上書き・重複作成を防ぐために使用。"""


@contextmanager
def session_lock(session_id: str):
    """同一セッションのhook実行を直列化する。"""
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe_session_key(session_id)}.lock"
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_session_lock(session_id: str, *, is_key: bool = False):
    """非ブロッキングでセッションロックを試み、取得結果をyieldする。"""
    lock_dir = STATE_DIR / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{session_id if is_key else safe_session_key(session_id)}.lock"
    try:
        lock_file = open(lock_path, "a", encoding="utf-8")
    except OSError:
        yield False
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_file.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


def load_state(session_id: str) -> dict:
    path = state_path(session_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("stateはJSONオブジェクトが必要です")
            return data
        except Exception as error:
            logger.info("stateファイルの読み込みに失敗しました（破損の可能性）: %s: %s", path, error)
            return {}
    return {}


def save_state(session_id: str, state: dict) -> None:
    """stateをatomicに保存する。"""
    state["last_used_at"] = format_iso(now_jst())
    atomic_write_text(state_path(session_id), json.dumps(state, ensure_ascii=False, indent=2))


def cleanup_old_states() -> None:
    """保持期間を過ぎたstateを、実行中セッションを保護しながら削除する。"""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now(JST) - timedelta(days=STATE_RETENTION_DAYS)
    removed = 0
    try:
        candidates = list(STATE_DIR.glob("*.json"))
    except OSError as error:
        logger.warning("state探索に失敗したためクリーンアップを見送ります: %s", error)
        return
    for path in candidates:
        with try_session_lock(path.stem, is_key=True) as acquired:
            if not acquired:
                continue
            try:
                if not path.exists():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                last_used = None
                if data.get("last_used_at"):
                    try:
                        last_used = datetime.fromisoformat(str(data["last_used_at"]))
                        if last_used.tzinfo is None:
                            last_used = last_used.replace(tzinfo=JST)
                    except ValueError:
                        pass
                if last_used is None:
                    last_used = datetime.fromtimestamp(path.stat().st_mtime, tz=JST)
                if last_used < cutoff:
                    path.unlink()
                    diagnostic = STATE_DIR / ".diagnostics" / (path.stem + ".status")
                    diagnostic.unlink(missing_ok=True)
                    removed += 1
            except Exception as error:
                logger.info("stateファイルのクリーンアップ中にスキップ: %s: %s", path.name, error)
    if removed > 0:
        logger.info("古いstateファイル %d 件を削除しました", removed)
