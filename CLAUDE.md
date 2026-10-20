# TaskManager

## A step's record reaches every reader in the same change

A change to what a step records, or where, updates every reader of that record in the same
change: the web detail, `tm render`, `tm task get --json`, export and restore, and the guides
that read it back. The web is a reader, not a follow-up.

Moving a review's findings from the `:review` section to `tm finding add` once shipped without
this. The web kept a flat findings table and lost the rounds the section had carried: which
review raised a finding, what the fix answered, and what a re-review reopened.

- What is true now, and how it got there, are two readers. A record that keeps only its latest
  state (a finding's answer, a node's verdict) still owes the reader the rounds before it. The
  web's Steps group reads them from the audit ledger (`web/steps.py`).
- A new table that holds a node's records gets a body part and a detail group. The table's test
  (`test_every_table_holding_a_nodes_records_has_a_body_part`) and the part's test
  (`every body part the server sends is drawn in the detail`) fail until both exist.
- A new ledger command written inside a step shows in Steps without any change to the web. Give
  it a reading in `stepItemHtml` when its raw payload does not read well.
- A web test of a new record is driven by the real CLI writing that record
  (`test_the_web_body_shows_each_step_who_ran_it_how_it_ended_and_what_it_recorded`), never only
  by a hand-written body.
