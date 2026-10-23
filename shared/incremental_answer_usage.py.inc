def _answer_usage_groups(messages: list[dict]) -> list[tuple[int, dict | None, dict | None]]:
    """User発話ごとにAgentMessageを回答単位へまとめる。"""
    groups: list[tuple[int, dict | None, dict | None]] = []
    turn_index = 0
    last_model = None
    last_turn_id = None
    for message in messages:
        turn_id = message.get("turn_id") or None
        if message.get("type") == "user_message":
            last_turn_id = turn_id
            turn_index += 1
            last_model = None
            continue
        if message.get("type") != "agent_message" or not str(message.get("message", "")).strip():
            continue
        if message.get("after_turn_boundary") or (turn_id and last_turn_id and turn_id != last_turn_id):
            turn_index += 1
            last_model = None
        last_turn_id = turn_id or last_turn_id
        model = format_model_name(message.get("model", ""), message.get("effort", ""))
        if groups and model == last_model:
            _, usage, subagent_usage = groups[-1]
            groups[-1] = (
                turn_index,
                _merge_usage_summaries(usage, message.get("usage")),
                _merge_usage_summaries(subagent_usage, message.get("subagent_usage")),
            )
        else:
            groups.append((turn_index, message.get("usage"), message.get("subagent_usage")))
            last_model = model
    return groups


def refresh_answer_usage_metadata(content: str, messages: list[dict]) -> str:
    """遅れて書かれたusage recordを既存assistant calloutへ反映する。

    本文・ユーザーcallout・既存のquota行は変更せず、assistant metadata内のUsage行だけを
    親ターンとモデルの連続区間に沿って差し替える。過去に同じ回答が複数calloutへ
    分割された場合は、その最後のcalloutにUsageを表示する。
    """
    groups = _answer_usage_groups(messages)
    spans = _assistant_header_spans(content)
    if not groups or not spans:
        return content
    lines = content.splitlines(keepends=True)
    user_positions = []
    active_fence = None
    for index, line in enumerate(lines):
        marker = _fence_marker(line)
        if active_fence is not None:
            if (
                marker is not None
                and marker[0] == active_fence[0]
                and marker[1] >= active_fence[1]
                and not marker[2].strip()
            ):
                active_fence = None
            continue
        if marker is not None:
            active_fence = marker[:2]
            continue
        if line.rstrip("\r\n") in ("> [!QUESTION] User", HIDDEN_USER_BOUNDARY):
            user_positions.append(index)

    groups_by_turn: dict[int, list[tuple[dict | None, dict | None]]] = {}
    for turn_index, usage, subagent_usage in groups:
        groups_by_turn.setdefault(turn_index, []).append((usage, subagent_usage))
    spans_by_turn: dict[int, list[tuple[int, int]]] = {}
    for start, end in spans:
        turn_index = bisect_right(user_positions, start)
        spans_by_turn.setdefault(turn_index, []).append((start, end))

    replacements = []
    for turn_index, turn_spans in spans_by_turn.items():
        runs: list[list[tuple[int, int]]] = []
        last_model = None
        for start, end in turn_spans:
            model = next((line for line in lines[start + 1:end] if line.startswith("> <small>🤖 ")), "")
            if runs and model == last_model:
                runs[-1].append((start, end))
            else:
                runs.append([(start, end)])
            last_model = model
        turn_groups = groups_by_turn.get(turn_index, [])
        if len(runs) != len(turn_groups):
            logger.warning("回答Usageのcallout対応を特定できません: turn=%s", turn_index)
            continue
        for run, (usage, subagent_usage) in zip(runs, turn_groups):
            usage_text = format_message_usage(usage, subagent_usage)
            for run_index, (start, end) in enumerate(run):
                metadata_lines = [
                    line for line in lines[start:end]
                    if not re.match(r"^> <small>Usage: .*?</small>\r?\n?$", line)
                ]
                if run_index == len(run) - 1 and usage_text:
                    newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
                    timing_index = next((index for index, line in enumerate(metadata_lines)
                                         if line.startswith("> <small>⏱ 回答所要時間: ")), len(metadata_lines))
                    metadata_lines.insert(timing_index, f"> <small>{usage_text}</small>{newline}")
                replacements.append((start, end, metadata_lines))
    for start, end, metadata_lines in sorted(replacements, reverse=True):
        lines[start:end] = metadata_lines
    return "".join(lines)
