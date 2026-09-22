Edge fixture 02: WAL backlog whose serialized size exceeds `CompileSettings.max_slice_bytes`
(default 40,000 bytes).

Bucket: edge. Labeler: deployer (KeenySpace v1 eval baseline).

Scenario: The wal.md contains 30 WAL entries of ~4KB each, totalling ~125KB. The coordinator
never feeds more than `max_slice_bytes` of serialized WAL to one pass: `extract_wal_slice`
stops at an entry boundary and reports `has_more`, which the coordinator surfaces as
`backlog_remaining` and answers with an immediate follow-up pass. The test replays that
cursor progression with the default settings and verifies the backlog is compiled in
several passes, no pass exceeds the byte budget, every entry is compiled exactly once in
id order, and the cursor ends at the last entry id (`expected_final_cursor`).

Regenerating wal.md: entries must be written with `keenyspace_server.wal.framing.format_entry`
(valid ULIDs, no empty `parent_id`) so `parse_wal` accepts them.
