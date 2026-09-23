# Express Sales Analytics — User Guide

A dashboard for reviewing sales, customers, product demand and forecasts from Express accounting reports.

**[Open the dashboard](https://express-analytics.vercel.app)** · [คู่มือภาษาไทย](docs/USER_GUIDE_TH.md)

## 1. Sign in

1. Open the dashboard in your browser.
2. Enter the email and password provided by your administrator.
3. Select **เข้าสู่ระบบ / Sign in**.

The menu depends on your role. If you can sign in but see no data, ask the administrator to assign your role and salesperson code. Do not share passwords.

### Forgot your password or want a new one?

- **Forgot password:** on the sign-in screen, select **ลืมรหัสผ่าน / Forgot password**, enter your account email and select **Send reset link**. Open the email link, enter the new password twice and save. Check spam if the email does not arrive. An expired link requires a fresh request.
- **Change password while signed in:** select **เปลี่ยนรหัสผ่าน / Change password** beneath your account in the sidebar. Enter the current password and the new password twice, then save.
- Use a memorable, hard-to-guess phrase of at least eight characters. Neither the administrator nor this guide needs your password. These actions do not change your role or visible sales data.
- Email recovery requires a deliverable mailbox and correctly configured Supabase email delivery and redirect URLs. See the [handover setup](docs/HANDOVER.md#password-recovery-email-setup).

## 2. Choose your language and theme

Use the buttons at the top of the screen:

- **English / ภาษาไทย** switches the interface language. Customer and product names remain as recorded in the source data.
- **Dark mode / โหมดมืด** and **Light mode / โหมดสว่าง** switch the display theme.

Your browser remembers these settings. Thai dates use Buddhist Era years, while English dates use Gregorian years; for example, **ธ.ค. 2568 = Dec 2025**. This changes only the display, not the stored dates or calculations.

## 3. Prepare the three Express reports

Export the following printed reports as **CSV files** from Express:

| Report | Thai report name | Document prefix |
|---|---|---|
| Cash sales | รายงานขายเงินสด | HS |
| Credit sales / invoices | รายงานใบกำกับสินค้า | IV |
| Deposit receipts | รายงานใบรับมัดจำ | AI |

Select the **same date range for all three reports**, covering the full reporting history you want to analyze. Upload the complete period each time, rather than only the latest month, because the dashboard summaries are rebuilt from the uploaded files.

Keep the original Express CSV files. Avoid opening and saving them in Excel before uploading: this can change their encoding or report layout. Renaming files is unnecessary; the system identifies report types from their contents.

## 4. Upload data — CEO only

1. Open **อัปโหลดข้อมูล / Upload data**.
2. Drag all three CSV files into the upload area, or click it to select them. File order does not matter.
3. Select **เริ่มนำเข้าข้อมูล / Start upload**.
4. Keep the page open while processing. The interface estimates approximately **20–60 seconds**; larger files may take longer.
5. Review the results before using the refreshed dashboard.

The upload result includes four checks:

| Check | What to review |
|---|---|
| Line reconciliation | The percentage of documents whose line amounts reconcile with their headers, accounting for vouchers, discounts and VAT rounding. |
| Unparsed rows | This should be **0**. Investigate any rows the parser could not read. |
| Duplicates handled | Review how duplicate records were handled and the unique document count. A nonzero duplicate count does not itself mean failure. |
| Date coverage | Confirm the first and last dates and any reported gaps match the period you intended to upload. |

Re-uploading the same report set updates matching document records instead of adding the same sales again. If a check fails, save the error message and confirm the report types and date ranges before exporting again.

**The dashboard does not synchronize with Express automatically.** Figures describe the dates covered by the most recently processed reports, not necessarily today's position.

## 5. Read the dashboard

| Page | How to use it |
|---|---|
| **ภาพรวม / Overview** | Review revenue excluding VAT and document counts. The **bar chart** shows total monthly revenue; the separate **line chart** shows average revenue per selling day. The monthly table gives the underlying figures. |
| **ยอดขาย / Sales** | Compare product groups and salespeople. Use the salesperson × month and salesperson × category tables. The **Other / อื่นๆ** chart band includes smaller groups so each bar represents the full monthly total within your scope. |
| **ความต้องการ / Demand** | Select a product group to see weekly quantities. Zero-sales weeks are included from its first sale onward; incomplete weeks are excluded. Check units before comparing quantities. |
| **พยากรณ์ / Forecast** | Review the next four weeks and the range derived from backtests. An overforecasting badge indicates that the historical range lies below the point forecast. Groups marked **หยุดขาย — ไม่แนะนำให้สั่ง** have stopped selling and should not be ordered from the displayed model estimates. |
| **สต็อก / Stock** | Compare actual warehouse stock with reorder points, and inspect products with no recent sales. Stock-check items are sorted by lifetime revenue. **Watch** means 60–89 days without sales; **risk / check recommended** means 90+ days. These are prompts to check physical stock. |
| **ความแม่นยำ / Accuracy** | Compare backtest error between stable and level-change periods. **Lower WAPE is better**; the shaded target is 10–15%. The group comparison uses the production model, an eight-week moving average (ma8). |
| **ลูกค้า / Customers** | Review customer segments based on recency, frequency and revenue. Start with **ลูกค้าที่ควรติดต่อ / Customers to contact** for displayed Cannot Lose customers who previously bought frequently or spent more but have gone quiet. |

On Overview, **สิ่งที่ต้องดูวันนี้ / What needs attention today** links to trend alerts and stock checks. It is shown only when your role can open the corresponding pages.

### Important labels

- **Change (per selling day)** compares revenue per selling day, not raw monthly revenue. Different numbers of selling days can make these trends differ.
- **ไม่ระบุ / Unassigned** means the source document has no salesperson code. These sales remain in the CEO's company totals.
- **Lifetime revenue** means revenue over the available report history, not necessarily the customer's or product's entire history.
- **Days since purchase / sale** are measured against the data's reporting date, not your computer's current date.
- A **dash (–)** can mean unavailable or insufficient data; it is not necessarily zero.
- Intermittent-demand groups show **Still selling / Stopped** rather than a trend percentage.

## 6. Who can see what?

| Role | Sales, demand and customer data | Additional pages |
|---|---|---|
| **Salesperson** | Only their own salesperson code. Customer revenue and RFM labels are calculated within that scope. | Overview, Sales, Demand and Customers. Forecast and Stock are disabled by default. |
| **Sales manager** | Only the salesperson codes assigned to their team. | Forecast, Stock and Accuracy are available; these currently display company-level planning figures, identified by the scope notice. |
| **CEO** | All company data, including documents without a salesperson code. | All pages, including Upload data and Users & permissions. |

Read the **data scope** notice when interpreting a page. A customer can have a different segment for a salesperson and the CEO because each ranking uses a different visible customer population.

### Assigning access — CEO only

After an administrator creates the account in Supabase, open **ผู้ใช้และสิทธิ์ / Users & permissions**:

1. Find the user's email.
2. Select the role.
3. For a salesperson, select their salesperson code. For a manager, select the team's codes.
4. Select **บันทึก / Save**.

Accounts without an assigned role cannot view dashboard data. Account creation, deletion and password resets are covered in the [handover guide](docs/HANDOVER.md).

## 7. What the system cannot tell you

- It has **sales data, not live warehouse inventory**. No recent sales can mean dormant stock or an empty shelf; inspect stock before acting.
- Forecasts and ranges are estimates, not guarantees or automatic purchase orders. Check actual stock, lead time and business circumstances before ordering.
- The initial ma8 backtest had **14.2% four-week WAPE in the earlier period** and **22.5% in the recent period**. These are historical errors, not promised future accuracy.
- The system does not update Express, place orders, or synchronize new transactions automatically.
- Customer names and product descriptions come from the reports. Do not assume similar names identify the same customer.

## 8. Troubleshooting

| Problem | What to do |
|---|---|
| Cannot sign in | Check your email and password; contact the administrator if a reset is needed. |
| Signed in, but no access | Ask the administrator to assign your role and salesperson code or team. |
| A page is missing from the menu | Check the role table above. Some pages are intentionally restricted. |
| Upload says a report is missing | Select all three required report types for the same period. |
| File cannot be read | Export a fresh CSV directly from Express without editing or resaving it. |
| Totals look wrong | Check the data scope, report date range and upload checks. Revenue is shown excluding VAT. |
| New controls are not visible | Refresh the page; if needed, use **Ctrl+F5** to reload the latest interface. |
| Website or data service is unavailable | Contact the owner to check hosting and database status. See the handover guide. |

## Further documentation

- [Thai user guide](docs/USER_GUIDE_TH.md)
- [Handover: accounts, hosting and maintenance](docs/HANDOVER.md)
- [Data cleaning and modelling methods](docs/METHODS.md)
- [Deployment instructions](docs/DEPLOY.md)

MFA and the deferred Batch 3 work remain future improvements.
