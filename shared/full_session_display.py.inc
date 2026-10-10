def _display_message_groups(messages: Iterable[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    for message in messages:
        if (
            message["type"] == "assistant"
            and groups
            and groups[-1][0]["type"] == "assistant"
            and groups[-1][0].get("model", "") == message.get("model", "")
            and groups[-1][0].get("turn_index") == message.get("turn_index")
        ):
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def _usage_records_by_display_group(
    groups: List[List[Dict[str, Any]]], usage_records: Iterable[Dict[str, Any]]
) -> List[List[Dict[str, Any]]]:
    """本文のないassistant usageを、同じユーザーターン内の最寄り回答calloutへ割り当てる。"""
    result: List[List[Dict[str, Any]]] = [[] for _ in groups]
    message_groups = {
        str(message.get("id")): group_index
        for group_index, group in enumerate(groups)
        for message in group
        if message.get("type") == "assistant" and message.get("id")
    }
    assistant_groups = [
        index for index, group in enumerate(groups)
        if group and group[0].get("type") == "assistant"
    ]
    for record in usage_records:
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message_id = str(record.get("id") or "")
        target = message_groups.get(message_id)
        if target is None:
            sequence = record.get("sequence")
            turn_index = record.get("turn_index")
            candidates = [
                index for index in assistant_groups
                if turn_index is None or groups[index][0].get("turn_index") == turn_index
            ]
            model = str(record.get("model") or "")
            same_model = [index for index in candidates if str(groups[index][0].get("model") or "") == model]
            if same_model:
                candidates = same_model
            if not candidates:
                continue

            def distance(group_index: int) -> Tuple[int, int, int]:
                positions = [
                    message.get("sequence") for message in groups[group_index]
                    if isinstance(message.get("sequence"), int)
                ]
                if not isinstance(sequence, int) or not positions:
                    return (0, group_index, group_index)
                start, end = min(positions), max(positions)
                gap = start - sequence if sequence < start else sequence - end if sequence > end else 0
                follows_record = 0 if start >= sequence else 1
                return (gap, follows_record, group_index)

            target = min(candidates, key=distance)
        result[target].append(record)
    return result
