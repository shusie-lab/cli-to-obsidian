
### 今日（最新順)
```dataview
TABLE WITHOUT ID
 round(sum(rows.usage.total_cost_usd),2) AS "総計$"
FROM "生成AI/ChatLog"
WHERE modified >= date(today)
WHERE usage.total_cost_usd != null
GROUP BY true
```
```dataview
TABLE WITHOUT ID
 ("[[" + file.path + "|" + truncate(file.name, 30) + "]]") AS "会話",
 truncate(title, 20) AS "タイトル",
 modified AS "更新日",
 message_count AS "発言数",
 split(file.folder, "/")[length(split(file.folder, "/")) - 1] AS "保存元",
 round(usage.total_cost_usd,2) AS "$",
 length(file.inlinks) AS "参照"
FROM "生成AI/ChatLog"
WHERE modified >= date(today)
SORT modified DESC
```

### 昨日（$の多い順)
```dataview
TABLE WITHOUT ID
 round(sum(rows.usage.total_cost_usd),2) AS "総計$"
FROM "生成AI/ChatLog"
WHERE modified >= date(today) - dur(1 day) AND modified < date(today)
WHERE usage.total_cost_usd != null
GROUP BY true
```
```dataview
TABLE WITHOUT ID
 ("[[" + file.path + "|" + truncate(file.name, 30) + "]]") AS "会話",
 truncate(title, 20) AS "タイトル",
 modified AS "更新日",
 message_count AS "発言数",
 split(file.folder, "/")[length(split(file.folder, "/")) - 1] AS "保存元",
 round(usage.total_cost_usd,2) AS "$",
 length(file.inlinks) AS "参照"
FROM "生成AI/ChatLog"
WHERE modified >= date(today) - dur(1 day) AND modified < date(today)
SORT usage.total_cost_usd DESC
```
