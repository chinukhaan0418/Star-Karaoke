MARKETING EMAIL RULES (the website itself sends no emails; use this when you send newsletters)

1. Only email people listed by:  python3 subscribers.py   (active opt-ins only).
   Never email people who unsubscribed, and never buy or scrape lists.
2. Use newsletter-template.html. The footer with the unsubscribe link, your company name
   and address must stay in EVERY marketing email. Replace {{UNSUBSCRIBE_URL}} with the
   personal link that subscribers.py prints for each person.
3. If your mail tool supports headers, also add (helps Gmail/Yahoo show an unsubscribe button):
       List-Unsubscribe: <https://YOURDOMAIN/unsubscribe.html?token=TOKEN>, <mailto:sanchiravarga52@gmail.com?subject=unsubscribe>
       List-Unsubscribe-Post: List-Unsubscribe=One-Click
   The one-click POST goes to https://YOURDOMAIN/api/unsubscribe?token=TOKEN (already built).
4. Honour unsubscribes immediately. Re-run subscribers.py before every send.
5. Booking confirmations/receipts (not promotional) don't need the unsubscribe link, but keep them
   free of advertising.
6. Anyone can type someone else's email into the form. Before your FIRST newsletter, consider
   sending a one-time "please confirm" email (double opt-in) and only keeping people who click.
