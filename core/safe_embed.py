from __future__ import annotations

import discord

_MAX_TOTAL = 6000
_MAX_TITLE = 256
_MAX_DESCRIPTION = 4096
_MAX_AUTHOR_NAME = 256
_MAX_FIELD_NAME = 256
_MAX_FIELD_VALUE = 1024
_MAX_FOOTER_TEXT = 2048
_ELLIPSIS = "…"


def _trunc(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + _ELLIPSIS if limit > 0 else ""


class SafeEmbed(discord.Embed):
    """Drop-in Embed replacement that enforces Discord character limits.

    Individual part limits are applied first, then parts are processed in
    render order (author → title → description → fields → footer) against
    the 6 000 total budget.  The first part that would exceed the budget is
    truncated; everything after it is dropped.
    """

    __slots__ = ()

    def to_dict(self):
        # --- 1. clamp individual parts to their own maximums ---
        self.title = _trunc(self.title, _MAX_TITLE)
        self.description = _trunc(self.description, _MAX_DESCRIPTION)

        try:
            author = self._author
            if author and "name" in author:
                author["name"] = _trunc(author["name"], _MAX_AUTHOR_NAME) or ""
        except AttributeError:
            pass

        try:
            footer = self._footer
            if footer and "text" in footer:
                footer["text"] = _trunc(footer["text"], _MAX_FOOTER_TEXT) or ""
        except AttributeError:
            pass

        for field in getattr(self, "_fields", []):
            field["name"] = _trunc(field["name"], _MAX_FIELD_NAME) or "\u200b"
            field["value"] = _trunc(field["value"], _MAX_FIELD_VALUE) or "\u200b"

        # --- 2. enforce 6 000 total in render order ---
        budget = _MAX_TOTAL

        # author name
        try:
            aname = self._author.get("name", "") if hasattr(self, "_author") else ""
        except AttributeError:
            aname = ""
        if aname:
            if len(aname) > budget:
                self._author["name"] = _trunc(aname, budget) or ""
                budget = 0
            else:
                budget -= len(aname)

        # title
        if budget <= 0:
            self.title = None
        elif self.title:
            if len(self.title) > budget:
                self.title = _trunc(self.title, budget)
            budget -= len(self.title or "")

        # description
        if budget <= 0:
            self.description = None
        elif self.description:
            if len(self.description) > budget:
                self.description = _trunc(self.description, budget)
            budget -= len(self.description or "")

        # fields
        if hasattr(self, "_fields"):
            keep = []
            for field in self._fields:
                if budget <= 0:
                    break
                name = field["name"]
                if len(name) > budget:
                    break
                budget -= len(name)

                value = field["value"]
                if len(value) > budget:
                    truncated = _trunc(value, budget)
                    if truncated and truncated != _ELLIPSIS:
                        field["value"] = truncated
                        budget = 0
                        keep.append(field)
                    # else: drop the field entirely, bare ellipsis is useless
                else:
                    budget -= len(value)
                    keep.append(field)
            self._fields = keep

        # footer
        try:
            ftext = self._footer.get("text", "") if hasattr(self, "_footer") else ""
        except AttributeError:
            ftext = ""
        if ftext:
            if budget <= 0:
                self._footer.pop("text", None)
            elif len(ftext) > budget:
                self._footer["text"] = _trunc(ftext, budget) or ""

        return super().to_dict()