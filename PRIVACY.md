# Privacy

This is a personal, non-commercial project built by one person for their own use.

- It accesses only my own Oura Ring data, with my own consent.
- Data is stored on a single server I rent from OVH, in a data centre in Canada (outside the EU). It was previously stored only on my own laptop. Access to the server is by my SSH key only, and its only open inbound port is SSH. The data files are readable only by the user that runs the bot.
- Data is not shared or sold. Two services receive limited data:
  - Telegram carries my own caffeine answers and the weekly message between the bot and my phone.
  - Once a week, aggregate statistics (counts, averages and ranges for the two groups of nights) are sent to Anthropic's API to write a short summary. No dates, no individual nights and no bedtimes are included, and the code refuses to send if a date is present.
- A nightly copy of the database is pulled from the server to my laptop and kept there (newest 30 copies). Nothing is sent to any other service.
- No other users' data is collected.

Terms: this app is provided as-is for personal use only and is not offered to the public.
