"""Pydantic models for LinkedIn Person/Profile data."""

import re
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator


class Interest(BaseModel):
    name: str
    category: str
    linkedin_url: Optional[str] = None


class Contact(BaseModel):
    type: str
    value: str
    label: Optional[str] = None


class Experience(BaseModel):
    """Work experience model."""

    position_title: Optional[str] = None
    institution_name: Optional[str] = None
    linkedin_url: Optional[str] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    duration: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None


class Education(BaseModel):
    """Education model."""

    institution_name: Optional[str] = None
    degree: Optional[str] = None
    linkedin_url: Optional[str] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    description: Optional[str] = None


class Accomplishment(BaseModel):
    category: str
    title: str
    issuer: Optional[str] = None
    issued_date: Optional[str] = None
    credential_id: Optional[str] = None
    credential_url: Optional[str] = None
    description: Optional[str] = None


class Person(BaseModel):
    """
    LinkedIn Person/Profile model with validation.

    Represents a complete LinkedIn profile with all scraped data.
    """

    linkedin_url: str
    name: Optional[str] = None
    location: Optional[str] = None
    about: Optional[str] = None
    open_to_work: bool = False
    experiences: List[Experience] = Field(default_factory=list)
    educations: List[Education] = Field(default_factory=list)
    interests: List[Interest] = Field(default_factory=list)
    accomplishments: List[Accomplishment] = Field(default_factory=list)
    contacts: List[Contact] = Field(default_factory=list)

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin_url(cls, v: str) -> str:
        """Validate that URL is a LinkedIn profile URL."""
        if "linkedin.com/in/" not in v:
            raise ValueError("Must be a valid LinkedIn profile URL (contains /in/)")
        return v

    def to_dict(self) -> dict:
        """
        Convert to dictionary.

        Returns:
            Dictionary representation of the person
        """
        return self.model_dump()

    def to_json(self, **kwargs) -> str:
        """
        Convert to JSON string.

        Args:
            **kwargs: Additional arguments for model_dump_json (e.g., indent=2)

        Returns:
            JSON string representation
        """
        return self.model_dump_json(**kwargs)

    @property
    def slug(self) -> str:
        """
        Filesystem-safe identifier derived from the profile URL, e.g.
        "https://www.linkedin.com/in/williamhgates/" -> "williamhgates".
        Falls back to a sanitized version of the name if the URL doesn't
        match the expected /in/<slug>/ shape.
        """
        match = re.search(r"/in/([^/?]+)", self.linkedin_url)
        if match:
            return match.group(1)
        fallback = re.sub(r"[^a-zA-Z0-9-]+", "-", self.name or "profile").strip("-")
        return fallback.lower() or "profile"

    def to_markdown(self) -> str:
        """
        Render this profile as a Markdown document.

        Returns:
            Markdown string suitable for writing to a .md file
        """
        lines = [f"# {self.name or 'Unknown'}", ""]
        lines.append(f"**LinkedIn:** {self.linkedin_url}")
        if self.location:
            lines.append(f"**Location:** {self.location}")
        if self.open_to_work:
            lines.append("**Open to work:** Yes")
        lines.append("")

        if self.about:
            lines.append("## About")
            lines.append("")
            lines.append(self.about)
            lines.append("")

        if self.experiences:
            lines.append("## Experience")
            lines.append("")
            for exp in self.experiences:
                header = exp.position_title or "Unknown position"
                if exp.institution_name:
                    header += f" — {exp.institution_name}"
                lines.append(f"### {header}")

                meta_parts = []
                if exp.from_date or exp.to_date:
                    meta_parts.append(f"{exp.from_date or '?'} – {exp.to_date or '?'}")
                if exp.duration:
                    meta_parts.append(exp.duration)
                if meta_parts:
                    lines.append(f"*{' · '.join(meta_parts)}*")
                if exp.location:
                    lines.append(f"Location: {exp.location}")
                if exp.linkedin_url:
                    lines.append(f"[Company on LinkedIn]({exp.linkedin_url})")

                lines.append("")
                if exp.description:
                    lines.append(exp.description)
                    lines.append("")

        if self.educations:
            lines.append("## Education")
            lines.append("")
            for edu in self.educations:
                header = edu.institution_name or "Unknown institution"
                if edu.degree:
                    header += f" — {edu.degree}"
                lines.append(f"### {header}")

                if edu.from_date or edu.to_date:
                    lines.append(f"*{edu.from_date or '?'} – {edu.to_date or '?'}*")
                if edu.linkedin_url:
                    lines.append(f"[School on LinkedIn]({edu.linkedin_url})")

                lines.append("")
                if edu.description:
                    lines.append(edu.description)
                    lines.append("")

        if self.interests:
            lines.append("## Interests")
            lines.append("")
            for interest in self.interests:
                lines.append(f"- {interest.name} ({interest.category})")
            lines.append("")

        if self.accomplishments:
            lines.append("## Accomplishments")
            lines.append("")
            for acc in self.accomplishments:
                header = f"- **{acc.title}**"
                if acc.issuer:
                    header += f" — {acc.issuer}"
                if acc.issued_date:
                    header += f" ({acc.issued_date})"
                lines.append(header)
            lines.append("")

        if self.contacts:
            lines.append("## Contact")
            lines.append("")
            for contact in self.contacts:
                label = f" ({contact.label})" if contact.label else ""
                lines.append(f"- **{contact.type}:** {contact.value}{label}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def save_markdown(self, output_dir: str) -> str:
        """
        Render this profile as Markdown and write it to `output_dir/<slug>.md`.

        Args:
            output_dir: Directory to write the file into (created if needed)

        Returns:
            Path to the written file
        """
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)

        filepath = directory / f"{self.slug}.md"
        filepath.write_text(self.to_markdown(), encoding="utf-8")

        return str(filepath)

    @property
    def company(self) -> Optional[str]:
        """
        Get the most recent company.

        Returns:
            Company name from most recent experience or None
        """
        if self.experiences:
            return self.experiences[0].institution_name
        return None

    @property
    def job_title(self) -> Optional[str]:
        """
        Get the most recent job title.

        Returns:
            Job title from most recent experience or None
        """
        if self.experiences:
            return self.experiences[0].position_title
        return None

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<Person {self.name}\n"
            f"  Company: {self.company}\n"
            f"  Title: {self.job_title}\n"
            f"  Location: {self.location}\n"
            f"  Experiences: {len(self.experiences)}\n"
            f"  Education: {len(self.educations)}>"
        )
