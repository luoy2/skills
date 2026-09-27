# Questions to the owner

Check every question to the owner against this list before sending it. The hook
registered in `.claude/settings.json` hands this file to the model before each
questionnaire, so keep it short.

1. Start with the current state and the mechanism, then what is to be decided.
   Read the state from the current code, configuration or command output and name
   the source; explain the mechanism in plain words, and gloss terms and ticket
   numbers.
2. When the recommended option defers, keeps an old path, hands the owner a step
   to do by hand, or files a ticket for later, its description gives the concrete
   reason: the dependency or window it waits for, the consumer still using the old
   path, why a tool cannot do the step, why the fix cannot happen in this change.
   Handing the owner a step needs an identity reason (a one-time code, an
   interactive login). Without a reason, recommend the other side: do it now,
   remove it, do it with the tool, fix it.
3. For a structural decision, each option states which dimension varies and how a
   new case enters (a declaration, configuration, a queue, a state machine), not
   only how to patch this one place.
4. Recommend one PR for one concern: its files, callers and old references
   together. Recommend splitting only when the parts have separate merge or
   acceptance gates.
5. When the same kind of problem shows up a second time, offer a structural audit
   among the options: a stronger model reviews, read-only, the design that produces
   it. Fix a small defect inside the current task instead of filing a ticket for it.
