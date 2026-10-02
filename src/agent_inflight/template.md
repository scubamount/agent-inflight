# In-flight work

What is open right now, across every agent session on this machine. Each
session starts with a brief built from `## Right now` (`inflight brief`) and
updates it at session end. Keep
entries short: state a future session needs, not a log of what happened.
`inflight trim --apply` archives old entries to `inflight-archive/`.

Entry format (newest first, one paragraph each):

    **YYYY-MM-DD HH:MM [session <id> #<entry-id>] — <thing>: <state>.** What is
    NOT done (unpushed, unrestarted, unverified), the next step, and who must decide.

Write one with `inflight add "<thing>: <state>" "<detail>"`; close it with
`inflight done <entry-id>`. Parked items
are entries too (`inflight add "parked: <thing>" "<trigger to revisit>"`);
other `## ` sections survive `trim` only while their header carries a recent date.

## Right now
