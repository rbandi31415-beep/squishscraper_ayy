"""Person/Profile scraper for LinkedIn."""

import logging
import re
from typing import Optional
from urllib.parse import urljoin
from playwright.async_api import Page

from .base import BaseScraper
from ..models import Person, Experience, Education, Accomplishment, Interest, Contact
from ..callbacks import ProgressCallback, SilentCallback
from ..core.exceptions import ScrapingError

logger = logging.getLogger(__name__)


class PersonScraper(BaseScraper):
    """Async scraper for LinkedIn person profiles."""

    def __init__(self, page: Page, callback: Optional[ProgressCallback] = None):
        """
        Initialize person scraper.

        Args:
            page: Playwright page object
            callback: Progress callback
        """
        super().__init__(page, callback)

    async def scrape(self, linkedin_url: str) -> Person:
        """
        Scrape a LinkedIn person profile.

        Args:
            linkedin_url: LinkedIn profile URL

        Returns:
            Person object with all scraped data

        Raises:
            AuthenticationError: If not logged in
            ScrapingError: If scraping fails
        """
        await self.callback.on_start("person", linkedin_url)

        try:
            # Navigate to profile first (this loads the page with our session)
            await self.navigate_and_wait(linkedin_url)
            await self.callback.on_progress("Navigated to profile", 10)

            # Now check if logged in
            await self.ensure_logged_in()

            # Wait for main content
            await self.page.wait_for_selector("main", timeout=10000)
            await self.wait_and_focus(1)

            # Get name and location
            name, location = await self._get_name_and_location()
            await self.callback.on_progress(f"Got name: {name}", 20)

            # Check open to work
            open_to_work = await self._check_open_to_work()

            # Get about
            about = await self._get_about()
            await self.callback.on_progress("Got about section", 30)

            # Scroll to load content
            await self.scroll_page_to_half()
            await self.scroll_page_to_bottom(pause_time=0.5, max_scrolls=3)

            # Get experiences
            experiences = await self._get_experiences(linkedin_url)
            await self.callback.on_progress(f"Got {len(experiences)} experiences", 60)

            educations = await self._get_educations(linkedin_url)
            await self.callback.on_progress(f"Got {len(educations)} educations", 50)

            interests = await self._get_interests(linkedin_url)
            await self.callback.on_progress(f"Got {len(interests)} interests", 65)

            accomplishments = await self._get_accomplishments(linkedin_url)
            await self.callback.on_progress(
                f"Got {len(accomplishments)} accomplishments", 85
            )

            contacts = await self._get_contacts(linkedin_url)
            await self.callback.on_progress(f"Got {len(contacts)} contacts", 95)

            person = Person(
                linkedin_url=linkedin_url,
                name=name,
                location=location,
                about=about,
                open_to_work=open_to_work,
                experiences=experiences,
                educations=educations,
                interests=interests,
                accomplishments=accomplishments,
                contacts=contacts,
            )

            await self.callback.on_progress("Scraping complete", 100)
            await self.callback.on_complete("person", person)

            return person

        except Exception as e:
            await self.callback.on_error(e)
            raise ScrapingError(f"Failed to scrape person profile: {e}")

    async def _get_name_and_location(self) -> tuple[str, Optional[str]]:
        """Extract name and location from profile."""
        try:
            name = await self.safe_extract_text("h1", default="")
            if not name:
                # LinkedIn no longer renders the profile name as <h1> (hashed
                # CSS-module classes, no stable hook) - it's the first <h2>
                # inside <main>, after a visually-hidden nav heading.
                for el in await self.page.locator("main h2").all():
                    text = await el.text_content()
                    if text and text.strip() and text.strip().lower() != "0 notifications":
                        name = text.strip()
                        break
            if not name:
                name = "Unknown"

            location = await self.safe_extract_text(
                ".text-body-small.inline.t-black--light.break-words", default=""
            )
            if not location:
                location = await self._get_location_fallback()

            return name, location if location else None
        except Exception as e:
            logger.warning(f"Error getting name/location: {e}")
            return "Unknown", None

    async def _get_location_fallback(self) -> Optional[str]:
        """
        Best-effort location lookup for the current top-card DOM, which has
        no stable class names. Headline formatting varies too much to key
        off ("Title @ Company" vs "Title, Company" vs plain text), so
        instead we use a structural anchor: location is reliably the
        paragraph immediately before a standalone "·" separator paragraph,
        e.g. "<p>United States</p><p>·</p><p><a>Contact info</a></p>".
        """
        try:
            name_heading = self.page.locator("main h2").first
            if await name_heading.count() == 0:
                return None
            top_card = name_heading.locator("xpath=ancestor::*[8]")
            if await top_card.count() == 0:
                return None

            paragraphs = await top_card.locator("p").all()
            for i, p in enumerate(paragraphs):
                text = await p.text_content()
                if text and text.strip() == "·" and i > 0:
                    prev_text = await paragraphs[i - 1].text_content()
                    if prev_text and prev_text.strip():
                        return prev_text.strip()
            return None
        except Exception:
            return None

    async def _check_open_to_work(self) -> bool:
        """Check if profile has open to work badge."""
        try:
            # Look for open to work indicator
            img_title = await self.get_attribute_safe(
                ".pv-top-card-profile-picture img", "title", default=""
            )
            return "#OPEN_TO_WORK" in img_title.upper()
        except:
            return False

    async def _get_about(self) -> Optional[str]:
        """Extract about section."""
        try:
            # Find the profile card that contains "About"
            profile_cards = await self.page.locator(
                '[data-view-name="profile-card"]'
            ).all()

            for card in profile_cards:
                card_text = await card.inner_text()
                # Check if this card contains "About" heading
                if card_text.strip().startswith("About"):
                    # Get the span with aria-hidden to avoid duplication
                    about_spans = await card.locator('span[aria-hidden="true"]').all()
                    # Skip the first span (it's the "About" heading), get the content
                    if len(about_spans) > 1:
                        about_text = await about_spans[1].text_content()
                        return about_text.strip() if about_text else None

            return None
        except Exception as e:
            logger.debug(f"Error getting about section: {e}")
            return None

    async def _get_description_for_item(self, item) -> Optional[str]:
        """
        Extract the expandable description/notes text under an experience
        entry, keyed off data-testid="expandable-text-box" - a stable,
        semantic attribute rather than a hashed CSS class.

        Strips out any nested "...more"/"...less" toggle button text by
        temporarily removing button descendants from the *live* element
        (not a detached clone) so innerText still reflects real layout and
        preserves paragraph breaks, then puts them back afterward so the
        page is left exactly as it was.
        """
        try:
            box = item.locator('[data-testid="expandable-text-box"]').first
            if await box.count() == 0:
                return None

            text = await box.evaluate(
                """el => {
                    const removed = Array.from(el.querySelectorAll('button'))
                        .map(b => ({ b, parent: b.parentNode, next: b.nextSibling }));
                    removed.forEach(({ b }) => b.remove());
                    const text = el.innerText;
                    removed.forEach(({ b, parent, next }) => {
                        if (next) parent.insertBefore(b, next);
                        else parent.appendChild(b);
                    });
                    return text;
                }"""
            )
            text = text.strip() if text else ""
            return text if text else None
        except Exception:
            return None

    async def _logo_name_for_item(self, item) -> Optional[str]:
        """
        Get a card's company/institution name from its logo's <img alt="X
        logo">. Nested sub-position items (multiple roles at one company,
        grouped under a parent card) have no logo of their own - only the
        parent card does - so this climbs to the nearest ancestor with a
        componentkey if the item itself has no image.
        """
        img = item.locator("img[alt]").first
        if await img.count() == 0:
            parent = item.locator("xpath=ancestor::*[@componentkey][1]")
            if await parent.count() > 0:
                img = parent.locator("img[alt]").first

        if await img.count() == 0:
            return None

        alt = await img.get_attribute("alt")
        if alt and alt.lower().endswith(" logo"):
            return alt[: -len(" logo")].strip()
        return None

    async def _find_section_container(self, heading, selector: str = "ul, ol", max_level: int = 8):
        """
        Find the section container for a main-page heading (e.g. Experience,
        Education) by climbing ancestors one level at a time and stopping at
        the first one containing a match for `selector`.

        This replaces an earlier open-ended 'ancestor::*[.//ul or .//ol][1]'
        xpath, which climbed with no distance limit: when a section's own
        list isn't a few levels up (e.g. Education using a different layout
        than Experience), it kept climbing until it hit *some* unrelated,
        distant list elsewhere on the page (the activity feed, "People you
        may know", etc.) and confidently returned the wrong section's items
        instead of failing cleanly.
        """
        for level in range(1, max_level + 1):
            ancestor = heading.locator(f"xpath=ancestor::*[{level}]")
            if await ancestor.count() == 0:
                break
            if await ancestor.locator(selector).count() > 0:
                return ancestor

        if selector == "ul, ol":
            fallback = heading.locator("xpath=ancestor::*[4]")
            if await fallback.count() > 0:
                return fallback
        return None

    async def _get_experiences_from_component_items(self, heading) -> list[Experience]:
        """
        Fallback for profiles where Experience entries aren't in a <ul>/<li>
        the section container's bounded climb can find - instead using the
        same componentkey^="entity-collection-item-" card marker as the
        Education fallback, with each field as a separate <p> tag (title,
        "Company · type", dates).

        Grouped/nested entries (multiple roles at one company) put their
        sub-roles in a nested <ul> inside the card; those are excluded here
        (via not(ancestor::ul)) since the aggregate company header itself
        has no single date range to anchor on - a known gap, same as the
        nested-position case found on other profiles.
        """
        experiences = []
        try:
            section = await self._find_section_container(
                heading, selector='[componentkey^="entity-collection-item-"]'
            )
            if section is None:
                return experiences

            items = await section.locator('[componentkey^="entity-collection-item-"]').all()

            for item in items:
                links = await item.locator('a').all()
                company_url = await links[0].get_attribute('href') if links else None

                logo_name = await self._logo_name_for_item(item)

                texts = []
                for p in await item.locator('xpath=.//p[not(ancestor::ul)]').all():
                    text = await p.text_content()
                    if text and text.strip():
                        texts.append(text.strip())

                if not texts:
                    continue

                date_idx = next(
                    (i for i, t in enumerate(texts) if self._looks_like_date_range(t)),
                    None,
                )
                if date_idx is None:
                    # No date range among top-level text (e.g. a grouped
                    # multi-role company header) - can't map to one entry.
                    continue

                pre_date = texts[:date_idx]
                position_title = pre_date[0] if pre_date else ""
                company_name = pre_date[1] if len(pre_date) > 1 else ""
                if "·" in company_name:
                    company_name = company_name.split("·")[0].strip()
                if company_name.strip().lower() in self._EMPLOYMENT_TYPES:
                    company_name = ""
                if not company_name and logo_name:
                    company_name = logo_name

                if not position_title:
                    continue

                work_times = texts[date_idx]
                from_date, to_date, duration = self._parse_work_times(work_times)
                description = await self._get_description_for_item(item)

                experiences.append(Experience(
                    position_title=position_title,
                    institution_name=company_name,
                    linkedin_url=company_url,
                    from_date=from_date,
                    to_date=to_date,
                    duration=duration,
                    location=None,
                    description=description,
                ))
        except Exception as e:
            logger.debug(f"Error in experience componentkey fallback: {e}")

        return experiences

    async def _get_experiences(self, base_url: str) -> list[Experience]:
        """Extract experiences from the main profile page Experience section."""
        experiences = []

        try:
            experience_heading = self.page.locator('h2:has-text("Experience")').first
            
            if await experience_heading.count() > 0:
                experience_section = await self._find_section_container(experience_heading)

                if experience_section is not None:
                    items = await experience_section.locator('ul > li, ol > li').all()
                    
                    for item in items:
                        try:
                            exp = await self._parse_main_page_experience(item)
                            if exp:
                                experiences.append(exp)
                        except Exception as e:
                            logger.debug(f"Error parsing experience from main page: {e}")
                            continue

            if not experiences and await experience_heading.count() > 0:
                experiences = await self._get_experiences_from_component_items(experience_heading)

            if not experiences:
                exp_url = urljoin(base_url, "details/experience")
                await self.navigate_and_wait(exp_url)
                await self.page.wait_for_selector("main", timeout=10000)
                await self.wait_and_focus(1.5)
                await self.scroll_page_to_half()
                await self.scroll_page_to_bottom(pause_time=0.5, max_scrolls=5)

                items = []
                main_element = self.page.locator('main')
                if await main_element.count() > 0:
                    list_items = await main_element.locator('list > listitem, ul > li').all()
                    if list_items:
                        items = list_items
                
                if not items:
                    old_list = self.page.locator(".pvs-list__container").first
                    if await old_list.count() > 0:
                        items = await old_list.locator(".pvs-list__paged-list-item").all()

                for item in items:
                    try:
                        result = await self._parse_experience_item(item)
                        if result:
                            if isinstance(result, list):
                                experiences.extend(result)
                            else:
                                experiences.append(result)
                    except Exception as e:
                        logger.debug(f"Error parsing experience item: {e}")
                        continue

        except Exception as e:
            logger.warning(
                f"Error getting experiences: {e}. The experience section may not be available or the page structure has changed."
            )

        return experiences
    
    async def _parse_main_page_experience(self, item) -> Optional[Experience]:
        """Parse experience from main profile page list item.

        LinkedIn sometimes wraps the whole card (logo + details) in a
        single <a>, and sometimes uses two separate links. Either way, the
        date range is the only reliably identifiable field by position, so
        text fields are classified relative to it rather than assumed by
        fixed index (grouped/nested-position cards have no separate company
        name text, which broke a purely positional mapping).
        """
        try:
            links = await item.locator('a').all()
            if not links:
                return None

            company_url = await links[0].get_attribute('href')
            detail_link = links[1] if len(links) > 1 else links[0]

            logo_name = await self._logo_name_for_item(item)

            # Prefer direct <p> tags when present - some profiles render
            # title/type/dates as clean separate paragraphs rather than the
            # <span aria-hidden="true"> structure _extract_unique_texts...
            # was built for. Without this, an outer wrapping <div> gets
            # matched instead (span-based extraction finds nothing to
            # anchor on) and all paragraphs' text comes back concatenated
            # into one blob, e.g. "TitleFull-timeMar 2024 - Present...".
            #
            # No ancestor::ul exclusion here (unlike the componentkey-based
            # method): this item is already one atomic <li> - possibly
            # itself a nested sub-role unpacked by the ul > li search - so
            # its own <p> tags are already properly scoped to just this
            # entry, with no further nested sub-list inside to worry about.
            direct_paragraphs = await detail_link.locator('p').all()
            if len(direct_paragraphs) >= 2:
                unique_texts = []
                for p in direct_paragraphs:
                    text = await p.text_content()
                    if text and text.strip():
                        unique_texts.append(text.strip())
            else:
                unique_texts = await self._extract_unique_texts_from_element(detail_link)

            if not unique_texts:
                return None

            # Merged-blob case: the outer wrapping span swallows all inner
            # text with no separators at all, e.g.
            # "Team 1 AmbassadorJan 2025 - Present · 1 yr 8 moPhiladelphia..."
            # Split it using the date range as an anchor point.
            if len(unique_texts) == 1:
                split = self._split_merged_experience_text(unique_texts[0])
                if split:
                    unique_texts = split

            date_idx = next(
                (i for i, t in enumerate(unique_texts) if self._looks_like_date_range(t)),
                None,
            )

            location = None
            if date_idx is None:
                position_title = unique_texts[0]
                company_name = unique_texts[1] if len(unique_texts) > 1 else ""
                work_times = unique_texts[2] if len(unique_texts) > 2 else ""
            else:
                pre_date = unique_texts[:date_idx]
                position_title = pre_date[0] if pre_date else ""
                company_name = pre_date[1] if len(pre_date) > 1 else ""
                work_times = unique_texts[date_idx]
                if len(unique_texts) > date_idx + 1:
                    location = unique_texts[date_idx + 1]

            # An employment-type word ("Full-time" etc.) isn't a company
            # name - some profiles render it as its own paragraph right
            # where company name would otherwise be.
            if company_name.strip().lower() in self._EMPLOYMENT_TYPES:
                company_name = ""

            # Grouped/nested-position cards have no separate company name
            # text on the main page - fall back to the logo's alt text.
            if not company_name and logo_name:
                company_name = logo_name

            if not position_title:
                return None

            from_date, to_date, duration = self._parse_work_times(work_times)
            description = await self._get_description_for_item(item)

            return Experience(
                position_title=position_title,
                institution_name=company_name,
                linkedin_url=company_url,
                from_date=from_date,
                to_date=to_date,
                duration=duration,
                location=location,
                description=description,
            )

        except Exception as e:
            logger.debug(f"Error parsing main page experience: {e}")
            return None

    _MONTH_ABBRS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

    _EMPLOYMENT_TYPES = frozenset({
        "full-time", "part-time", "self-employed", "freelance",
        "contract", "internship", "apprenticeship", "seasonal",
    })

    _DATE_CHUNK_RE = re.compile(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\s*-\s*"
        r"(?:Present|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})"
        r"(?:\s*·\s*\d+\s*(?:yrs?|mos?)(?:\s*\d+\s*(?:yrs?|mos?))?)?",
        re.IGNORECASE,
    )

    def _split_merged_experience_text(self, text: str) -> Optional[list]:
        """
        Split a merged "TitleDATE_RANGELocation" blob (no separators between
        fields) into [title, date_range, location], using the date range as
        an anchor. Returns None if no date range pattern is found, meaning
        this text isn't this merged-blob shape.
        """
        match = self._DATE_CHUNK_RE.search(text)
        if not match:
            return None

        title = text[: match.start()].strip()
        date_range = match.group().strip()
        location = text[match.end():].strip()

        if not title:
            return None

        parts = [title, date_range]
        if location:
            parts.append(location)
        return parts

    def _looks_like_date_range(self, text: str) -> bool:
        """Heuristic: does this text look like a work/education date range?"""
        if not text:
            return False
        t = text.lower()
        if "present" in t:
            return True
        if any(m in t for m in self._MONTH_ABBRS):
            return True
        if re.search(r"\b(19|20)\d{2}\b", t):
            return True
        return False
    
    async def _extract_unique_texts_from_element(self, element) -> list[str]:
        """Extract unique text content from nested elements, avoiding duplicates from parent/child overlap."""
        text_elements = await element.locator('span[aria-hidden="true"], div > span').all()
        
        if not text_elements:
            text_elements = await element.locator('span, div').all()
        
        seen_texts = set()
        unique_texts = []
        
        for el in text_elements:
            text = await el.text_content()
            if text and text.strip():
                text = text.strip()
                if text not in seen_texts and len(text) < 200 and not any(text in t or t in text for t in seen_texts if len(t) > 3):
                    seen_texts.add(text)
                    unique_texts.append(text)
        
        return unique_texts

    async def _parse_experience_item(self, item):
        """Parse experience item. Returns Experience or list for nested positions."""
        try:
            links = await item.locator('a, link').all()
            if len(links) >= 2:
                company_url = await links[0].get_attribute('href')
                detail_link = links[1]
                
                generics = await detail_link.locator('generic, span, div').all()
                texts = []
                for g in generics:
                    text = await g.text_content()
                    if text and text.strip() and len(text.strip()) < 200:
                        texts.append(text.strip())
                
                unique_texts = list(dict.fromkeys(texts))
                
                if len(unique_texts) >= 2:
                    position_title = unique_texts[0]
                    company_name = unique_texts[1]
                    work_times = unique_texts[2] if len(unique_texts) > 2 else ""
                    location = unique_texts[3] if len(unique_texts) > 3 else ""
                    
                    from_date, to_date, duration = self._parse_work_times(work_times)
                    
                    return Experience(
                        position_title=position_title,
                        institution_name=company_name,
                        linkedin_url=company_url,
                        from_date=from_date,
                        to_date=to_date,
                        duration=duration,
                        location=location,
                        description=None,
                    )
            
            entity = item.locator('div[data-view-name="profile-component-entity"]').first
            if await entity.count() == 0:
                return None

            children = await entity.locator("> *").all()
            if len(children) < 2:
                return None

            company_link = children[0].locator("a").first
            company_url = await company_link.get_attribute("href")

            detail_container = children[1]
            detail_children = await detail_container.locator("> *").all()

            if len(detail_children) == 0:
                return None

            has_nested_positions = False
            if len(detail_children) > 1:
                nested_list = await detail_children[1].locator(".pvs-list__container").count()
                has_nested_positions = nested_list > 0

            if has_nested_positions:
                return await self._parse_nested_experience(item, company_url, detail_children)
            else:
                first_detail = detail_children[0]
                nested_elements = await first_detail.locator("> *").all()

                if len(nested_elements) == 0:
                    return None

                span_container = nested_elements[0]
                outer_spans = await span_container.locator("> *").all()

                position_title = ""
                company_name = ""
                work_times = ""
                location = ""

                if len(outer_spans) >= 1:
                    aria_span = outer_spans[0].locator('span[aria-hidden="true"]').first
                    position_title = await aria_span.text_content()
                if len(outer_spans) >= 2:
                    aria_span = outer_spans[1].locator('span[aria-hidden="true"]').first
                    company_name = await aria_span.text_content()
                if len(outer_spans) >= 3:
                    aria_span = outer_spans[2].locator('span[aria-hidden="true"]').first
                    work_times = await aria_span.text_content()
                if len(outer_spans) >= 4:
                    aria_span = outer_spans[3].locator('span[aria-hidden="true"]').first
                    location = await aria_span.text_content()

                from_date, to_date, duration = self._parse_work_times(work_times)

                description = ""
                if len(detail_children) > 1:
                    description = await detail_children[1].inner_text()

                return Experience(
                    position_title=position_title.strip(),
                    institution_name=company_name.strip(),
                    linkedin_url=company_url,
                    from_date=from_date,
                    to_date=to_date,
                    duration=duration,
                    location=location.strip(),
                    description=description.strip() if description else None,
                )

        except Exception as e:
            logger.debug(f"Error parsing experience: {e}")
            return None

    async def _parse_nested_experience(
        self, item, company_url: str, detail_children
    ) -> list[Experience]:
        """
        Parse nested experience positions (multiple roles at the same company).
        Returns a list of Experience objects.
        """
        experiences = []

        try:
            # Get company name from first detail
            first_detail = detail_children[0]
            nested_elements = await first_detail.locator("> *").all()
            if len(nested_elements) == 0:
                return []

            span_container = nested_elements[0]
            outer_spans = await span_container.locator("> *").all()

            # First span is company name for nested positions
            company_name = ""
            if len(outer_spans) >= 1:
                aria_span = outer_spans[0].locator('span[aria-hidden="true"]').first
                company_name = await aria_span.text_content()

            # Get the nested list from detail_children[1]
            nested_container = detail_children[1].locator(".pvs-list__container").first
            nested_items = await nested_container.locator(
                ".pvs-list__paged-list-item"
            ).all()

            for nested_item in nested_items:
                try:
                    # Each nested item has a link with position details
                    link = nested_item.locator("a").first
                    link_children = await link.locator("> *").all()

                    if len(link_children) == 0:
                        continue

                    # Navigate to get the spans
                    first_child = link_children[0]
                    nested_els = await first_child.locator("> *").all()
                    if len(nested_els) == 0:
                        continue

                    spans_container = nested_els[0]
                    position_spans = await spans_container.locator("> *").all()

                    # Extract position details
                    position_title = ""
                    work_times = ""
                    location = ""

                    if len(position_spans) >= 1:
                        aria_span = (
                            position_spans[0].locator('span[aria-hidden="true"]').first
                        )
                        position_title = await aria_span.text_content()
                    if len(position_spans) >= 2:
                        aria_span = (
                            position_spans[1].locator('span[aria-hidden="true"]').first
                        )
                        work_times = await aria_span.text_content()
                    if len(position_spans) >= 3:
                        aria_span = (
                            position_spans[2].locator('span[aria-hidden="true"]').first
                        )
                        location = await aria_span.text_content()

                    # Parse dates
                    from_date, to_date, duration = self._parse_work_times(work_times)

                    # Get description if available
                    description = ""
                    if len(link_children) > 1:
                        description = await link_children[1].inner_text()

                    experiences.append(
                        Experience(
                            position_title=position_title.strip(),
                            institution_name=company_name.strip(),
                            linkedin_url=company_url,
                            from_date=from_date,
                            to_date=to_date,
                            duration=duration,
                            location=location.strip(),
                            description=description.strip() if description else None,
                        )
                    )

                except Exception as e:
                    logger.debug(f"Error parsing nested position: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Error parsing nested experience: {e}")

        return experiences

    def _parse_work_times(
        self, work_times: str
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Parse work times string into from_date, to_date, duration.

        Examples:
        - "2000 - Present · 26 yrs 1 mo" -> ("2000", "Present", "26 yrs 1 mo")
        - "Jan 2020 - Dec 2022 · 2 yrs" -> ("Jan 2020", "Dec 2022", "2 yrs")
        - "2015 - Present" -> ("2015", "Present", None)
        """
        if not work_times:
            return None, None, None

        try:
            # Split by · to separate date range from duration
            parts = work_times.split("·")
            times = parts[0].strip() if len(parts) > 0 else ""
            duration = parts[1].strip() if len(parts) > 1 else None

            # Parse dates - split by " - " to get from and to
            if " - " in times:
                date_parts = times.split(" - ")
                from_date = date_parts[0].strip()
                to_date = date_parts[1].strip() if len(date_parts) > 1 else ""
            else:
                from_date = times
                to_date = ""

            return from_date, to_date, duration
        except Exception as e:
            logger.debug(f"Error parsing work times '{work_times}': {e}")
            return None, None, None

    # LinkedIn renders education date ranges with an en-dash ("1999 – 2001"),
    # not the plain hyphen _parse_education_times() splits on, so this is
    # parsed directly via capture groups rather than routed through that
    # helper.
    _YEAR_RANGE_RE = re.compile(r"(\d{4})\s*[–-]\s*(\d{4})|(\d{4})")

    async def _get_educations_from_school_links(self) -> list[Education]:
        """
        Fallback for profiles where Education has no <ul>/<li> markup: each
        entry is instead two sibling <a> tags (a logo-only link and a
        detail link with name/degree/dates all merged into one blob, no
        separators) sharing the same /school/ href. The logo link's <img
        alt="X logo"> gives a clean institution name that sidesteps the
        merged-text ambiguity; degree/dates are split out of the detail
        link's blob using the institution name as a known prefix plus a
        year-range as an anchor.
        """
        educations = []
        try:
            links = await self.page.locator('main a[href*="/school/"]').all()

            href_to_links: dict = {}
            ordered_hrefs = []
            for link in links:
                href = await link.get_attribute('href')
                if not href:
                    continue
                if href not in href_to_links:
                    href_to_links[href] = []
                    ordered_hrefs.append(href)
                href_to_links[href].append(link)

            for href in ordered_hrefs:
                pair = href_to_links[href]

                logo_name = None
                detail_link = None
                detail_len = -1
                for link in pair:
                    img = link.locator("img[alt]").first
                    if await img.count() > 0:
                        alt = await img.get_attribute("alt")
                        if alt and alt.lower().endswith(" logo"):
                            logo_name = alt[: -len(" logo")].strip()
                    text = await link.text_content()
                    length = len(text.strip()) if text else 0
                    if length > detail_len:
                        detail_len = length
                        detail_link = link

                if detail_link is None or detail_len == 0:
                    continue

                blob = (await detail_link.text_content() or "").strip()

                remainder = blob
                institution_name = logo_name
                if institution_name and blob.startswith(institution_name):
                    remainder = blob[len(institution_name):]
                elif not institution_name:
                    # No logo alt text available - can't cleanly separate
                    # institution from degree, so keep the whole blob as
                    # institution_name rather than guess wrong.
                    institution_name = blob
                    remainder = ""

                degree = None
                from_date = None
                to_date = None
                if remainder:
                    match = self._YEAR_RANGE_RE.search(remainder)
                    if match:
                        degree = remainder[: match.start()].strip()
                        if match.group(1) and match.group(2):
                            from_date, to_date = match.group(1), match.group(2)
                        else:
                            from_date = to_date = match.group(3)
                    else:
                        degree = remainder.strip()

                educations.append(Education(
                    institution_name=institution_name,
                    degree=degree if degree else None,
                    linkedin_url=href,
                    from_date=from_date,
                    to_date=to_date,
                    description=None,
                ))
        except Exception as e:
            logger.debug(f"Error in education school-link fallback: {e}")

        return educations

    async def _get_educations_from_component_items(self, heading) -> list[Education]:
        """
        Fallback for profiles where Education entries have no clickable
        link at all (this happens once a profile has enough entries that
        LinkedIn shows a "Show all N educations" summary card instead of
        individually-linked items) but still render full text as separate
        <p> tags. LinkedIn marks every repeated card item generically -
        regardless of section or whether it's link-wrapped - with a
        componentkey starting "entity-collection-item-" (sometimes with a
        second hyphen depending on the generated hash, so matched by
        single-hyphen prefix), which is a much more stable hook here than
        trying to key off links or a specific DOM shape.
        """
        educations = []
        try:
            section = await self._find_section_container(
                heading, selector='[componentkey^="entity-collection-item-"]'
            )
            if section is None:
                return educations

            items = await section.locator('[componentkey^="entity-collection-item-"]').all()

            for item in items:
                texts = []
                for p in await item.locator("p").all():
                    text = await p.text_content()
                    if text and text.strip():
                        texts.append(text.strip())

                if not texts:
                    continue

                institution_name = texts[0]
                degree = None
                from_date = None
                to_date = None

                rest = texts[1:]
                date_idx = next(
                    (i for i, t in enumerate(rest) if self._YEAR_RANGE_RE.search(t)),
                    None,
                )
                if date_idx is not None:
                    if date_idx > 0:
                        degree = rest[0]
                    match = self._YEAR_RANGE_RE.search(rest[date_idx])
                    if match.group(1) and match.group(2):
                        from_date, to_date = match.group(1), match.group(2)
                    else:
                        from_date = to_date = match.group(3)
                elif rest:
                    degree = rest[0]

                educations.append(Education(
                    institution_name=institution_name,
                    degree=degree,
                    linkedin_url=None,
                    from_date=from_date,
                    to_date=to_date,
                    description=None,
                ))
        except Exception as e:
            logger.debug(f"Error in education componentkey fallback: {e}")

        return educations

    async def _get_educations(self, base_url: str) -> list[Education]:
        """Extract educations from the main profile page Education section."""
        educations = []

        try:
            education_heading = self.page.locator('h2:has-text("Education")').first
            
            if await education_heading.count() > 0:
                education_section = await self._find_section_container(education_heading)

                if education_section is not None:
                    items = await education_section.locator('ul > li, ol > li').all()
                    
                    for item in items:
                        try:
                            edu = await self._parse_main_page_education(item)
                            if edu:
                                educations.append(edu)
                        except Exception as e:
                            logger.debug(f"Error parsing education from main page: {e}")
                            continue

            if not educations:
                # Some profiles render Education with no <ul>/<li> at all -
                # just two sibling <a> tags per entry (logo link + detail
                # link) sharing the same /school/... href, with no list
                # wrapper. /school/ links only ever appear in this section
                # (the top-card summary line is plain text, not a link), so
                # searching page-wide is safe here.
                educations = await self._get_educations_from_school_links()

            if not educations and await education_heading.count() > 0:
                # Some profiles (once there are 3+ entries) render Education
                # with no clickable link at all - just plain text in a
                # repeated card structure.
                educations = await self._get_educations_from_component_items(education_heading)

            if not educations:
                edu_url = urljoin(base_url, "details/education")
                await self.navigate_and_wait(edu_url)
                await self.page.wait_for_selector("main", timeout=10000)
                await self.wait_and_focus(2)
                await self.scroll_page_to_half()
                await self.scroll_page_to_bottom(pause_time=0.5, max_scrolls=5)

                items = []
                main_element = self.page.locator('main')
                if await main_element.count() > 0:
                    list_items = await main_element.locator('ul > li, ol > li').all()
                    if list_items:
                        items = list_items
                
                if not items:
                    old_list = self.page.locator(".pvs-list__container").first
                    if await old_list.count() > 0:
                        items = await old_list.locator(".pvs-list__paged-list-item").all()

                for item in items:
                    try:
                        edu = await self._parse_education_item(item)
                        if edu:
                            educations.append(edu)
                    except Exception as e:
                        logger.debug(f"Error parsing education item: {e}")
                        continue

        except Exception as e:
            logger.warning(
                f"Error getting educations: {e}. The education section may not be publicly visible or the page structure has changed."
            )

        return educations
    
    async def _parse_main_page_education(self, item) -> Optional[Education]:
        """Parse education from main profile page list item with [logo_link, details_link] structure."""
        try:
            links = await item.locator('a').all()
            if not links:
                return None
            
            institution_url = await links[0].get_attribute('href')
            detail_link = links[1] if len(links) > 1 else links[0]
            
            unique_texts = await self._extract_unique_texts_from_element(detail_link)
            
            if not unique_texts:
                return None
            
            institution_name = unique_texts[0]
            degree = None
            times = ""
            
            if len(unique_texts) == 3:
                degree = unique_texts[1]
                times = unique_texts[2]
            elif len(unique_texts) == 2:
                second = unique_texts[1]
                if " - " in second or any(c.isdigit() for c in second):
                    times = second
                else:
                    degree = second
            
            from_date, to_date = self._parse_education_times(times)
            
            return Education(
                institution_name=institution_name,
                degree=degree.strip() if degree else None,
                linkedin_url=institution_url,
                from_date=from_date,
                to_date=to_date,
                description=None,
            )
            
        except Exception as e:
            logger.debug(f"Error parsing main page education: {e}")
            return None

    async def _parse_education_item(self, item) -> Optional[Education]:
        """Parse a single education item."""
        try:
            links = await item.locator('a, link').all()
            if len(links) >= 1:
                institution_url = await links[0].get_attribute('href')
                
                detail_link = links[1] if len(links) >= 2 else links[0]
                generics = await detail_link.locator('generic, span, div').all()
                texts = []
                for g in generics:
                    text = await g.text_content()
                    if text and text.strip() and len(text.strip()) < 200:
                        texts.append(text.strip())
                
                unique_texts = list(dict.fromkeys(texts))
                
                if unique_texts:
                    institution_name = unique_texts[0]
                    degree = None
                    times = ""
                    
                    if len(unique_texts) == 3:
                        degree = unique_texts[1]
                        times = unique_texts[2]
                    elif len(unique_texts) == 2:
                        second = unique_texts[1]
                        if " - " in second or second.isdigit() or any(c.isdigit() for c in second):
                            times = second
                        else:
                            degree = second
                    
                    from_date, to_date = self._parse_education_times(times)
                    
                    return Education(
                        institution_name=institution_name,
                        degree=degree.strip() if degree else None,
                        linkedin_url=institution_url,
                        from_date=from_date,
                        to_date=to_date,
                        description=None,
                    )
            
            entity = item.locator('div[data-view-name="profile-component-entity"]').first
            if await entity.count() == 0:
                return None

            children = await entity.locator("> *").all()
            if len(children) < 2:
                return None

            institution_link = children[0].locator("a").first
            institution_url = await institution_link.get_attribute("href")

            detail_container = children[1]
            detail_children = await detail_container.locator("> *").all()

            if len(detail_children) == 0:
                return None

            first_detail = detail_children[0]
            nested_elements = await first_detail.locator("> *").all()

            if len(nested_elements) == 0:
                return None

            span_container = nested_elements[0]
            outer_spans = await span_container.locator("> *").all()

            institution_name = ""
            degree = None
            times = ""

            if len(outer_spans) >= 1:
                aria_span = outer_spans[0].locator('span[aria-hidden="true"]').first
                institution_name = await aria_span.text_content()

            if len(outer_spans) == 3:
                aria_span = outer_spans[1].locator('span[aria-hidden="true"]').first
                degree = await aria_span.text_content()
                aria_span = outer_spans[2].locator('span[aria-hidden="true"]').first
                times = await aria_span.text_content()
            elif len(outer_spans) == 2:
                aria_span = outer_spans[1].locator('span[aria-hidden="true"]').first
                times = await aria_span.text_content()

            from_date, to_date = self._parse_education_times(times)

            description = ""
            if len(detail_children) > 1:
                description = await detail_children[1].inner_text()

            return Education(
                institution_name=institution_name.strip(),
                degree=degree.strip() if degree else None,
                linkedin_url=institution_url,
                from_date=from_date,
                to_date=to_date,
                description=description.strip() if description else None,
            )

        except Exception as e:
            logger.debug(f"Error parsing education: {e}")
            return None

    def _parse_education_times(self, times: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parse education times string into from_date, to_date.

        Examples:
        - "1973 - 1977" -> ("1973", "1977")
        - "2015" -> ("2015", "2015")
        - "" -> (None, None)
        """
        if not times:
            return None, None

        try:
            # Split by " - " to get from and to dates
            if " - " in times:
                parts = times.split(" - ")
                from_date = parts[0].strip()
                to_date = parts[1].strip() if len(parts) > 1 else ""
            else:
                # Single year
                from_date = times.strip()
                to_date = times.strip()

            return from_date, to_date
        except Exception as e:
            logger.debug(f"Error parsing education times '{times}': {e}")
            return None, None

    async def _get_interests(self, base_url: str) -> list[Interest]:
        """Extract interests from the main profile page Interests section with tablist."""
        interests = []

        try:
            interests_heading = self.page.locator('h2:has-text("Interests")').first
            
            if await interests_heading.count() > 0:
                interests_section = interests_heading.locator('xpath=ancestor::*[.//tablist or .//*[@role="tablist"]][1]')
                if await interests_section.count() == 0:
                    interests_section = interests_heading.locator('xpath=ancestor::*[4]')
                
                tabs = await interests_section.locator('[role="tab"], tab').all() if await interests_section.count() > 0 else []
                
                if tabs:
                    for tab in tabs:
                        try:
                            tab_name = await tab.text_content()
                            if not tab_name:
                                continue
                            tab_name = tab_name.strip()
                            category = self._map_interest_tab_to_category(tab_name)

                            await tab.click()
                            await self.wait_and_focus(0.5)

                            tabpanel = interests_section.locator('[role="tabpanel"]').first
                            if await tabpanel.count() > 0:
                                list_items = await tabpanel.locator('li, listitem').all()
                                
                                for item in list_items:
                                    try:
                                        interest = await self._parse_interest_item(item, category)
                                        if interest:
                                            interests.append(interest)
                                    except Exception as e:
                                        logger.debug(f"Error parsing interest item: {e}")
                                        continue
                        except Exception as e:
                            logger.debug(f"Error processing interest tab: {e}")
                            continue
            
            if not interests:
                interests_url = urljoin(base_url, "details/interests/")
                await self.navigate_and_wait(interests_url)
                await self.page.wait_for_selector("main", timeout=10000)
                await self.wait_and_focus(1.5)

                tabs = await self.page.locator('[role="tab"], tab').all()

                if not tabs:
                    logger.debug("No interests tabs found on profile")
                    return interests

                for tab in tabs:
                    try:
                        tab_name = await tab.text_content()
                        if not tab_name:
                            continue
                        tab_name = tab_name.strip()
                        category = self._map_interest_tab_to_category(tab_name)

                        await tab.click()
                        await self.wait_and_focus(0.8)

                        tabpanel = self.page.locator('[role="tabpanel"], tabpanel').first
                        list_items = await tabpanel.locator("listitem, li, .pvs-list__paged-list-item").all()

                        for item in list_items:
                            try:
                                interest = await self._parse_interest_item(item, category)
                                if interest:
                                    interests.append(interest)
                            except Exception as e:
                                logger.debug(f"Error parsing interest item: {e}")
                                continue

                    except Exception as e:
                        logger.debug(f"Error processing interest tab: {e}")
                        continue

        except Exception as e:
            logger.warning(f"Error getting interests: {e}")

        return interests
    
    async def _parse_interest_item(self, item, category: str) -> Optional[Interest]:
        """Parse a single interest item from profile or details page."""
        try:
            link = item.locator("a, link").first
            if await link.count() == 0:
                return None
            href = await link.get_attribute("href")

            unique_texts = await self._extract_unique_texts_from_element(item)
            name = unique_texts[0] if unique_texts else None

            if name and href:
                return Interest(
                    name=name,
                    category=category,
                    linkedin_url=href,
                )
            return None
        except Exception as e:
            logger.debug(f"Error parsing interest: {e}")
            return None

    def _map_interest_tab_to_category(self, tab_name: str) -> str:
        tab_lower = tab_name.lower()
        if "compan" in tab_lower:
            return "company"
        elif "group" in tab_lower:
            return "group"
        elif "school" in tab_lower:
            return "school"
        elif "newsletter" in tab_lower:
            return "newsletter"
        elif "voice" in tab_lower or "influencer" in tab_lower:
            return "influencer"
        else:
            return tab_lower

    async def _get_accomplishments(self, base_url: str) -> list[Accomplishment]:
        accomplishments = []

        accomplishment_sections = [
            ("certifications", "certification"),
            ("honors", "honor"),
            ("publications", "publication"),
            ("patents", "patent"),
            ("courses", "course"),
            ("projects", "project"),
            ("languages", "language"),
            ("organizations", "organization"),
        ]

        for url_path, category in accomplishment_sections:
            try:
                section_url = urljoin(base_url, f"details/{url_path}/")
                await self.navigate_and_wait(section_url)
                await self.page.wait_for_selector("main", timeout=10000)
                await self.wait_and_focus(1)

                nothing_to_see = await self.page.locator(
                    'text="Nothing to see for now"'
                ).count()
                if nothing_to_see > 0:
                    continue

                main_list = self.page.locator(
                    ".pvs-list__container, main ul, main ol"
                ).first
                if await main_list.count() == 0:
                    continue

                items = await main_list.locator(".pvs-list__paged-list-item").all()
                if not items:
                    items = await main_list.locator("> li").all()

                seen_titles = set()
                for item in items:
                    try:
                        accomplishment = await self._parse_accomplishment_item(
                            item, category
                        )
                        if accomplishment and accomplishment.title not in seen_titles:
                            seen_titles.add(accomplishment.title)
                            accomplishments.append(accomplishment)
                    except Exception as e:
                        logger.debug(f"Error parsing {category} item: {e}")
                        continue

            except Exception as e:
                logger.debug(f"Error getting {category}s: {e}")
                continue

        return accomplishments

    async def _parse_accomplishment_item(
        self, item, category: str
    ) -> Optional[Accomplishment]:
        try:
            entity = item.locator(
                'div[data-view-name="profile-component-entity"]'
            ).first
            if await entity.count() > 0:
                spans = await entity.locator('span[aria-hidden="true"]').all()
            else:
                spans = await item.locator('span[aria-hidden="true"]').all()

            title = ""
            issuer = ""
            issued_date = ""
            credential_id = ""

            for i, span in enumerate(spans[:5]):
                text = await span.text_content()
                if not text:
                    continue
                text = text.strip()

                if len(text) > 500:
                    continue

                if i == 0:
                    title = text
                elif "Issued by" in text:
                    parts = text.split("·")
                    issuer = parts[0].replace("Issued by", "").strip()
                    if len(parts) > 1:
                        issued_date = parts[1].strip()
                elif "Issued " in text and not issued_date:
                    issued_date = text.replace("Issued ", "")
                elif "Credential ID" in text:
                    credential_id = text.replace("Credential ID ", "")
                elif i == 1 and not issuer:
                    issuer = text
                elif (
                    any(
                        month in text
                        for month in [
                            "Jan",
                            "Feb",
                            "Mar",
                            "Apr",
                            "May",
                            "Jun",
                            "Jul",
                            "Aug",
                            "Sep",
                            "Oct",
                            "Nov",
                            "Dec",
                        ]
                    )
                    and not issued_date
                ):
                    if "·" in text:
                        parts = text.split("·")
                        issued_date = parts[0].strip()
                    else:
                        issued_date = text

            link = item.locator('a[href*="credential"], a[href*="verify"]').first
            credential_url = (
                await link.get_attribute("href") if await link.count() > 0 else None
            )

            if not title or len(title) > 200:
                return None

            return Accomplishment(
                category=category,
                title=title,
                issuer=issuer if issuer else None,
                issued_date=issued_date if issued_date else None,
                credential_id=credential_id if credential_id else None,
                credential_url=credential_url,
            )

        except Exception as e:
            logger.debug(f"Error parsing accomplishment: {e}")
            return None

    async def _get_contacts(self, base_url: str) -> list[Contact]:
        """Extract contact info from the contact-info overlay dialog."""
        contacts = []

        try:
            contact_url = urljoin(base_url, "overlay/contact-info/")
            await self.navigate_and_wait(contact_url)
            await self.wait_and_focus(1)

            dialog = self.page.locator('dialog, [role="dialog"]').first
            if await dialog.count() == 0:
                logger.warning("Contact info dialog not found")
                return contacts

            contact_sections = await dialog.locator('h3').all()
            
            for section_heading in contact_sections:
                try:
                    heading_text = await section_heading.text_content()
                    if not heading_text:
                        continue
                    heading_text = heading_text.strip().lower()
                    
                    section_container = section_heading.locator('xpath=ancestor::*[1]')
                    if await section_container.count() == 0:
                        continue
                    
                    contact_type = self._map_contact_heading_to_type(heading_text)
                    if not contact_type:
                        continue
                    
                    links = await section_container.locator('a').all()
                    for link in links:
                        href = await link.get_attribute('href')
                        text = await link.text_content()
                        if href and text:
                            text = text.strip()
                            label = None
                            sibling_text = await section_container.locator('span, generic').all()
                            for sib in sibling_text:
                                sib_text = await sib.text_content()
                                if sib_text and sib_text.strip().startswith('(') and sib_text.strip().endswith(')'):
                                    label = sib_text.strip()[1:-1]
                                    break
                            
                            if contact_type == "linkedin":
                                contacts.append(Contact(type=contact_type, value=href, label=label))
                            elif contact_type == "email" and "mailto:" in href:
                                contacts.append(Contact(type=contact_type, value=href.replace("mailto:", ""), label=label))
                            else:
                                contacts.append(Contact(type=contact_type, value=text, label=label))
                    
                    if contact_type == "birthday" and not links:
                        birthday_text = await section_container.text_content()
                        if birthday_text:
                            birthday_value = birthday_text.replace(heading_text, "").replace("Birthday", "").strip()
                            if birthday_value:
                                contacts.append(Contact(type="birthday", value=birthday_value))
                    
                    if contact_type == "phone" and not links:
                        phone_text = await section_container.text_content()
                        if phone_text:
                            phone_value = phone_text.replace(heading_text, "").replace("Phone", "").strip()
                            if phone_value:
                                contacts.append(Contact(type="phone", value=phone_value))
                    
                    if contact_type == "address" and not links:
                        address_text = await section_container.text_content()
                        if address_text:
                            address_value = address_text.replace(heading_text, "").replace("Address", "").strip()
                            if address_value:
                                contacts.append(Contact(type="address", value=address_value))
                                
                except Exception as e:
                    logger.debug(f"Error parsing contact section: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Error getting contacts: {e}")

        return contacts
    
    def _map_contact_heading_to_type(self, heading: str) -> Optional[str]:
        """Map contact section heading to contact type."""
        heading = heading.lower()
        if "profile" in heading:
            return "linkedin"
        elif "website" in heading:
            return "website"
        elif "email" in heading:
            return "email"
        elif "phone" in heading:
            return "phone"
        elif "twitter" in heading or "x.com" in heading:
            return "twitter"
        elif "birthday" in heading:
            return "birthday"
        elif "address" in heading:
            return "address"
        return None
