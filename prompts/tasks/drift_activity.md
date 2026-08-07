# Drift 原子活动任务

根据指定 activity 产出一个内部主动候选，不直接面向用户发送。

- candidate_aggregation 只能归纳输入候选的共同信息，不得新增外部事实；parent_candidate_ids 必须来自输入且至少包含两个不同 ID，evidence_refs 必须为每个父候选引用至少一个 source_ref。
- conversation_topic_preparation 只能准备一个低打扰话题，evidence_refs 必须引用输入中的 message_id、fact_id 或 memory_id。记忆若与近期消息冲突，以近期消息为准。
- conversation_topic_preparation 不得填写 parent_candidate_ids；同一组证据只准备一个话题，不靠改写标题制造重复候选。
- title 和 summary 应是内部候选描述，不得声称用户已回复或未回复。
- 只输出 JSON：
  `{"title":"候选标题","summary":"候选内容","parent_candidate_ids":[],"evidence_refs":[]}`。
