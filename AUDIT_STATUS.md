# Strict requirements audit — work in progress

This branch is NOT ready to merge or deploy yet.

The uploaded ZIP could not be opened because the execution environment failed before Python started. The code audit therefore uses the connected GitHub repository, not a verified byte-for-byte copy of that ZIP. Main is unchanged.

The first GitHub Actions run did not execute any steps. No runtime, browser, import or live OpenAI test has passed in this audit. Changes on this branch are unverified until the test suite runs successfully.

Confirmed static defects in the original code: ignored seasonality workbooks and growth coefficients; category not used as a policy input; all inbound quantities counted without arrival dates; blank stock values marked known; loss of trailing underscores in SKUs; no server-side approval/edit workflow; unprotected partner data; incomplete customer-level anomaly support.

External facts still required: fresh IEK stock, lead times, anonymous customer IDs, exact stockout periods, category policies, unit conversions and the accepted 1C import specification.
