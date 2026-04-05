"""
PostIQ Email Report — sends HTML payment report via AWS SES.
"""
from datetime import datetime

RECIPIENTS = [
    "hannah@greatoakcounseling.com",
    "travis@greatoakcounseling.com",
]
SENDER = "travis@greatoakcounseling.com"


def send_report(results, duplicates, outstanding_balances=None, dry_run=False):
    """Build and send the PostIQ Payment Report email via SES."""
    from scripts.aws import _ensure_boto3, boto3

    _ensure_boto3()
    ses = boto3.client("ses", region_name="us-east-1")

    # Categorize results
    posted = [r for r in results if r["status"] == "OK" and r.get("method") == "V2"]
    fallback = [r for r in results if r["status"] == "OK" and r.get("method") == "V1"]
    var_posted = [r for r in results if r["status"] == "OK" and r.get("method", "").endswith("-VAR")]
    flagged = [r for r in results if r["status"] == "FLAGGED"]
    failed = [r for r in results if r["status"] in ("FAILED", "TIMEOUT")]
    outstanding = sorted(outstanding_balances) if outstanding_balances else []

    all_ok = posted + fallback + var_posted
    total_amount = sum(float(r["amount"]) for r in all_ok)
    report_date = datetime.now().strftime("%A, %B %d, %Y")
    short_date = datetime.now().strftime("%m/%d/%Y")

    has_errors = bool(flagged or failed)
    subject = f"PostIQ Payment Report -- {short_date}"
    if dry_run:
        subject = f"[DRY RUN] {subject}"
    if has_errors:
        subject += " -- ERRORS DETECTED"

    html = _build_html(
        report_date=report_date,
        posted=posted,
        fallback=fallback,
        var_posted=var_posted,
        flagged=flagged,
        failed=failed,
        outstanding=outstanding,
        total_amount=total_amount,
        dry_run=dry_run,
    )

    try:
        ses.send_email(
            Source=SENDER,
            Destination={"ToAddresses": RECIPIENTS},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": html, "Charset": "UTF-8"}},
            },
        )
        print(f"Email report sent to {', '.join(RECIPIENTS)}")
    except Exception as e:
        print(f"WARNING: Email send failed: {e}")
        print("  (Ensure SES sender/recipients are verified)")


def _build_html(report_date, posted, fallback, var_posted, flagged, failed,
                outstanding, total_amount, dry_run):
    """Build the HTML email matching the PostIQ report design."""
    posted_count = len(posted) + len(fallback)
    failed_count = len(failed)
    fallback_count = len(fallback)
    outstanding_count = len(outstanding)

    mode_label = " (DRY RUN)" if dry_run else ""

    var_count = len(var_posted)

    # --- Completed Payments table rows ---
    all_ok = sorted(posted + fallback + var_posted, key=lambda r: r["name"])
    completed_rows = ""
    for i, r in enumerate(all_ok):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        completed_rows += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):.2f}</td>'
            f'</tr>\n'
        )

    # --- Failed / Manual Posting table rows ---
    failed_rows = ""
    for i, r in enumerate(flagged + failed):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        reason = r.get("reason", "Unknown error")
        failed_rows += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):.2f}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{reason}</td>'
            f'</tr>\n'
        )

    # --- Verify Allocation (fallback) table rows ---
    fallback_rows = ""
    for i, r in enumerate(fallback):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        appt_date = r.get("date", "")
        fallback_rows += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):.2f}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{appt_date}</td>'
            f'</tr>\n'
        )

    # --- Outstanding Balances rows ---
    outstanding_rows = ""
    for i, name in enumerate(outstanding):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        outstanding_rows += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{name}</td>'
            f'</tr>\n'
        )

    # --- Build sections ---
    failed_section = ""
    if flagged or failed:
        failed_section = f'''
        <h2 style="color:#b71c1c;font-size:18px;margin:30px 0 5px 0;padding-bottom:5px;border-bottom:2px solid #b71c1c;">
            Action Required -- Manual Posting Needed
        </h2>
        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
            <tr style="background:#8b0000;">
                <th style="padding:10px 12px;text-align:left;color:white;font-weight:600;">Client</th>
                <th style="padding:10px 12px;text-align:right;color:white;font-weight:600;">Amount</th>
                <th style="padding:10px 12px;text-align:left;color:white;font-weight:600;">Reason</th>
            </tr>
            {failed_rows}
        </table>
        '''

    fallback_section = ""
    if fallback:
        fallback_section = f'''
        <h2 style="color:#6a1b9a;font-size:18px;margin:30px 0 5px 0;padding-bottom:5px;border-bottom:2px solid #6a1b9a;">
            Verify Allocation -- Payments Posted via Fallback
        </h2>
        <p style="color:#555;font-size:14px;margin-bottom:10px;">
            These payments were posted to the correct client but could <strong>not</strong> be matched
            to a specific appointment date. Please verify in TherapyAppointment that each payment is
            allocated to the correct clinician and appointment.
        </p>
        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
            <tr style="background:#6a1b9a;">
                <th style="padding:10px 12px;text-align:left;color:white;font-weight:600;">Client</th>
                <th style="padding:10px 12px;text-align:right;color:white;font-weight:600;">Amount</th>
                <th style="padding:10px 12px;text-align:left;color:white;font-weight:600;">Expected Appt Date</th>
            </tr>
            {fallback_rows}
        </table>
        '''

    # --- Name Variation table rows ---
    var_rows = ""
    for i, r in enumerate(var_posted):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        reason = r.get("reason", "")
        var_rows += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):.2f}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{reason}</td>'
            f'</tr>\n'
        )

    var_section = ""
    if var_posted:
        var_section = f'''
        <h2 style="color:#1565c0;font-size:18px;margin:30px 0 5px 0;padding-bottom:5px;border-bottom:2px solid #1565c0;">
            Posted via Name Variation -- Verify Allocation
        </h2>
        <p style="color:#555;font-size:14px;margin-bottom:10px;">
            These payments were posted using a nickname or alias. Please verify the correct client
            was matched in TherapyAppointment.
        </p>
        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
            <tr style="background:#1565c0;">
                <th style="padding:10px 12px;text-align:left;color:white;font-weight:600;">Client</th>
                <th style="padding:10px 12px;text-align:right;color:white;font-weight:600;">Amount</th>
                <th style="padding:10px 12px;text-align:left;color:white;font-weight:600;">Name Used</th>
            </tr>
            {var_rows}
        </table>
        '''

    outstanding_section = ""
    if outstanding:
        outstanding_section = f'''
        <h2 style="color:#e65100;font-size:18px;margin:30px 0 5px 0;padding-bottom:5px;border-bottom:2px solid #e65100;">
            Outstanding Balances -- Follow Up Needed
        </h2>
        <p style="color:#555;font-size:14px;margin-bottom:10px;">
            These clients have additional open charges beyond the most recent session.
            Today's payment was posted, but the remaining balance needs attention.
        </p>
        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
            <tr style="background:#d84315;">
                <th style="padding:10px 12px;text-align:left;color:white;font-weight:600;">Client</th>
            </tr>
            {outstanding_rows}
        </table>
        '''

    html = f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:20px 0;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:4px;overflow:hidden;">

    <!-- Header -->
    <tr>
        <td style="background:#2e4a2e;padding:25px 30px;">
            <h1 style="color:#ffffff;margin:0;font-size:24px;font-weight:700;">PostIQ Payment Report</h1>
            <p style="color:#c8e6c9;margin:5px 0 0 0;font-size:14px;">Square Payments -- {report_date}{mode_label}</p>
        </td>
    </tr>

    <!-- Summary Stats -->
    <tr>
        <td style="padding:25px 30px 10px 30px;">
            <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                    <td width="25%" style="text-align:center;padding:10px;">
                        <div style="font-size:32px;font-weight:700;color:#2e7d32;">{posted_count}</div>
                        <div style="font-size:12px;color:#666;margin-top:2px;">Posted</div>
                    </td>
                    <td width="25%" style="text-align:center;padding:10px;">
                        <div style="font-size:32px;font-weight:700;color:#b71c1c;">{failed_count}</div>
                        <div style="font-size:12px;color:#666;margin-top:2px;">Need Manual Posting</div>
                    </td>
                    <td width="25%" style="text-align:center;padding:10px;">
                        <div style="font-size:32px;font-weight:700;color:#6a1b9a;">{fallback_count}</div>
                        <div style="font-size:12px;color:#666;margin-top:2px;">Verify Allocation</div>
                    </td>
                    <td width="25%" style="text-align:center;padding:10px;">
                        <div style="font-size:32px;font-weight:700;color:#e65100;">{outstanding_count}</div>
                        <div style="font-size:12px;color:#666;margin-top:2px;">Outstanding Balances</div>
                    </td>
                </tr>
            </table>
        </td>
    </tr>

    <!-- Body -->
    <tr>
        <td style="padding:10px 30px 30px 30px;">

            <!-- Completed Payments -->
            <h2 style="color:#2e7d32;font-size:18px;margin:20px 0 10px 0;padding-bottom:5px;border-bottom:2px solid #2e7d32;">
                Completed Payments
            </h2>
            <table style="width:100%;border-collapse:collapse;margin-bottom:10px;">
                <tr style="background:#2e4a2e;">
                    <th style="padding:10px 12px;text-align:left;color:white;font-weight:600;">Client</th>
                    <th style="padding:10px 12px;text-align:right;color:white;font-weight:600;">Amount</th>
                </tr>
                {completed_rows}
                <tr style="background:#2e4a2e;">
                    <td style="padding:10px 12px;color:white;font-weight:700;">Total</td>
                    <td style="padding:10px 12px;text-align:right;color:white;font-weight:700;">${total_amount:,.2f}</td>
                </tr>
            </table>

            {failed_section}
            {fallback_section}
            {var_section}
            {outstanding_section}

            <!-- Sign-off -->
            <p style="color:#555;font-size:14px;margin-top:30px;">
                Thanks for your attention to detail and getting these tasks completed.
            </p>
            <p style="color:#333;font-size:14px;">
                <strong>Oakley</strong>, Great Oak Counseling's AI Assistant
            </p>

        </td>
    </tr>

    <!-- Footer -->
    <tr>
        <td style="background:#2e4a2e;padding:12px 30px;text-align:center;">
            <p style="color:#c8e6c9;margin:0;font-size:12px;">PostIQ -- automated payment posting by Great Oak Counseling</p>
        </td>
    </tr>

</table>
</td></tr></table>
</body>
</html>'''

    return html
