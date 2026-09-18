# Report design notes

## Who reads this

One page for IDinsight's fundraising director. She has minutes, not an hour, and
no interest in how the scoring works. The page answers one question: what should
I chase today.

## Layout decisions

**Tiers before dates.** Opportunities are grouped High, Medium and Lower
priority rather than listed chronologically. High and Medium open on load and
Lower sits behind a closed `details` element, so the first screen holds only
what is worth acting on. An empty High section still renders, because "nothing
urgent today" is useful news.

**The rationale is the loudest body text.** Each card leads with the title as
the link to the notice, then one meta line (source, relative publication date,
deadline, countries, value), then the rationale at full size. The rationale is
what saves her opening the notice, so it outranks the notice text, which stays
collapsed.

**Colour carries meaning only.** Tier colour sits on the left border and the
score badge; red appears only for a deadline inside seven days. That warning is
bold as well as red, so it survives a greyscale print and reaches a screen
reader. Every colour pair clears WCAG AA in light and dark mode.

**Filters, not settings.** A search box and one checkbox per source, both acting
on the rendered page in vanilla JavaScript. Searching opens collapsed sections,
so a match is never buried. Nothing to configure.

**Self contained.** One file, inline CSS, nothing fetched, 16px gutters and no
sideways scroll on a phone.
