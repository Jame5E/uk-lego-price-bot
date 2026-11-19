from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord

from .db import TrackedItem
from .embeds import (
    effective_notify_user_ids,
    format_discount_short,
    format_item_title,
    format_pings,
    make_error_embed,
    make_status_embed,
)
from .uk_finder import default_box_art_url
from .wishlist_store import remove_wishlist_entry, upsert_wishlist_entry

if TYPE_CHECKING:
    from .bot import LegoTrackerBot


class DealLinkView(discord.ui.View):
    """Native link button attached to deal alerts."""

    def __init__(self, url: str, retailer_name: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label=f"View at {retailer_name}",
                emoji="🛒",
                style=discord.ButtonStyle.link,
                url=url,
            )
        )


class _PingUserSelect(discord.ui.UserSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="1. Pick a user to manage their pings...",
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, PingManagerView)
        view.target_user = self.values[0]
        view.rebuild_items()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class _PingSetSelect(discord.ui.Select):
    def __init__(
        self,
        options: list[discord.SelectOption],
        chunk_keys: set[str],
        *,
        placeholder: str,
        row: int,
    ) -> None:
        self.chunk_keys = chunk_keys
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, PingManagerView)
        if not view.target_user:
            await interaction.response.send_message(
                embed=make_error_embed("Pick a User", "Please pick a user first."),
                ephemeral=True,
            )
            return
        selected_keys = set(self.values)
        uid = str(view.target_user.id)
        for item in view.bot.db.list_items():
            key = item.set_number or str(item.id)
            if key not in self.chunk_keys:
                continue
            current = effective_notify_user_ids(item, view.bot.config.user_id, view.bot.config.extra_notify_user_ids)
            want = key in selected_keys
            changed = False
            if want and uid not in current:
                current.append(uid)
                changed = True
            elif not want and uid in current:
                current.remove(uid)
                changed = True
            elif not item.notify_user_ids:
                changed = True

            if changed:
                updated = view.bot.db.update_item(item.id, notify_user_ids=current)
                if updated and updated.set_number:
                    upsert_wishlist_entry(
                        view.bot.config.wishlist_path,
                        {
                            "name": updated.name,
                            "set_number": updated.set_number,
                            "notify_user_ids": list(updated.notify_user_ids),
                        },
                    )
        view.rebuild_items()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PingManagerView(discord.ui.View):
    def __init__(self, bot: "LegoTrackerBot", target_user: discord.abc.User | None = None) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.target_user = target_user
        self.rebuild_items()

    def rebuild_items(self) -> None:
        self.clear_items()
        self.add_item(_PingUserSelect())

        items = self.bot.db.list_items()
        if self.target_user and items:
            uid = str(self.target_user.id)
            chunks = [items[i : i + 25] for i in range(0, min(len(items), 75), 25)]
            for idx, chunk in enumerate(chunks):
                options: list[discord.SelectOption] = []
                chunk_keys: set[str] = set()
                for item in chunk:
                    key = item.set_number or str(item.id)
                    chunk_keys.add(key)
                    current = effective_notify_user_ids(
                        item, self.bot.config.user_id, self.bot.config.extra_notify_user_ids
                    )
                    options.append(
                        discord.SelectOption(
                            label=format_item_title(item)[:100],
                            value=key,
                            description=(item.category or "LEGO Set")[:100],
                            default=(uid in current),
                        )
                    )
                start_n = idx * 25 + 1
                end_n = idx * 25 + len(chunk)
                placeholder = (
                    "2. Check/uncheck sets for this user..."
                    if len(chunks) == 1
                    else f"2. Check/uncheck sets ({start_n}–{end_n})..."
                )
                self.add_item(_PingSetSelect(options, chunk_keys, placeholder=placeholder, row=1 + idx))

            btn_row = 1 + len(chunks)
            add_all_btn = discord.ui.Button(
                label="Add to ALL",
                emoji="✅",
                style=discord.ButtonStyle.success,
                row=btn_row,
            )
            remove_all_btn = discord.ui.Button(
                label="Remove from ALL",
                emoji="🔕",
                style=discord.ButtonStyle.danger,
                row=btn_row,
            )

            async def _on_add_all(interaction: discord.Interaction) -> None:
                await self._bulk_set(interaction, add=True)

            async def _on_remove_all(interaction: discord.Interaction) -> None:
                await self._bulk_set(interaction, add=False)

            add_all_btn.callback = _on_add_all
            remove_all_btn.callback = _on_remove_all
            self.add_item(add_all_btn)
            self.add_item(remove_all_btn)

    async def _bulk_set(self, interaction: discord.Interaction, *, add: bool) -> None:
        if not self.target_user:
            return
        uid = str(self.target_user.id)
        for item in self.bot.db.list_items():
            current = effective_notify_user_ids(
                item, self.bot.config.user_id, self.bot.config.extra_notify_user_ids
            )
            if add and uid not in current:
                current.append(uid)
            elif not add and uid in current:
                current.remove(uid)
            updated = self.bot.db.update_item(item.id, notify_user_ids=current)
            if updated and updated.set_number:
                upsert_wishlist_entry(
                    self.bot.config.wishlist_path,
                    {
                        "name": updated.name,
                        "set_number": updated.set_number,
                        "notify_user_ids": list(updated.notify_user_ids),
                    },
                )
        self.rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def build_embed(self) -> discord.Embed:
        items = self.bot.db.list_items()
        if not items:
            return make_status_embed("🔔 Manage Pings", "Your wishlist is currently empty.")
        if not self.target_user:
            return make_status_embed(
                "🔔 Interactive Ping Manager",
                "Pick a Discord user from the dropdown below to check/uncheck which LEGO sets they get pinged for.",
                color=discord.Color.blue(),
            )
        uid = str(self.target_user.id)
        lines = []
        for item in items:
            current = effective_notify_user_ids(
                item, self.bot.config.user_id, self.bot.config.extra_notify_user_ids
            )
            mark = "✅" if uid in current else "⬜"
            lines.append(f"{mark} **{format_item_title(item)}**")
        return make_status_embed(
            "🔔 Interactive Ping Manager",
            (
                f"Managing pings for <@{uid}>\n"
                f"Use the dropdown below to check/uncheck sets, or click **Add to ALL** / **Remove from ALL**:\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.blue(),
            footer="Changes save immediately • Menu stays active for 3 minutes",
        )


class CheckResultView(discord.ui.View):
    """Interactive buttons attached to live price-check embeds."""

    def __init__(
        self,
        bot: "LegoTrackerBot",
        set_number: str | None,
        *,
        deal_url: str | None = None,
        retailer_name: str | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.set_number = set_number
        if deal_url:
            self.add_item(
                discord.ui.Button(
                    label=f"View at {retailer_name or 'Store'}",
                    emoji="🛒",
                    style=discord.ButtonStyle.link,
                    url=deal_url,
                )
            )
        if not set_number:
            self.recheck_btn.disabled = True

    @discord.ui.button(label="Re-check", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def recheck_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.set_number:
            await interaction.response.send_message("Set number unavailable.", ephemeral=True)
            return
        await interaction.response.defer()
        embeds = await self.bot.run_price_checks(set_number=self.set_number, force_refresh=True)
        if embeds:
            await interaction.edit_original_response(embed=embeds[0], view=self)

    @discord.ui.button(label="Manage Pings", emoji="🔔", style=discord.ButtonStyle.primary)
    async def pings_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = PingManagerView(self.bot, target_user=interaction.user)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


class EditSetModal(discord.ui.Modal):
    """Modal popup to edit a set's alert settings (or a user's personal threshold)."""

    def __init__(
        self,
        bot: "LegoTrackerBot",
        item: TrackedItem,
        detail_view: "SetDetailView | None" = None,
        *,
        for_user_id: str | None = None,
    ) -> None:
        title_prefix = "My Threshold" if for_user_id else "Edit"
        super().__init__(title=f"{title_prefix} #{item.set_number or item.id}")
        self.bot = bot
        self.item = item
        self.detail_view = detail_view
        self.for_user_id = for_user_id

        cur_target = item.target_price_gbp
        cur_disc = item.min_discount_pct
        if for_user_id:
            u_thresh = bot.db.get_user_thresholds(item.id).get(for_user_id)
            if u_thresh:
                cur_target = u_thresh[0]
                if u_thresh[1] is not None:
                    cur_disc = u_thresh[1]

        self.target_input = discord.ui.TextInput(
            label="Target Price (£, 0 or blank to clear)",
            placeholder="e.g. 320.00",
            default=f"{cur_target:.2f}" if cur_target else "",
            required=False,
            max_length=12,
        )
        self.discount_input = discord.ui.TextInput(
            label="Minimum Discount (% off RRP)",
            placeholder="e.g. 15",
            default=f"{cur_disc:.0f}",
            required=True,
            max_length=5,
        )
        self.add_item(self.target_input)
        self.add_item(self.discount_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_target = self.target_input.value.strip()
        raw_disc = self.discount_input.value.strip()
        try:
            target_val: float | None = float(raw_target) if raw_target else 0.0
            if target_val is not None and (target_val < 0 or target_val > 5000):
                raise ValueError("Target price must be between £0 and £5000.")
            disc_val = float(raw_disc)
            if disc_val < 0 or disc_val > 90:
                raise ValueError("Minimum discount must be between 0% and 90%.")
        except ValueError as exc:
            await interaction.response.send_message(
                embed=make_error_embed("Invalid Input", str(exc)),
                ephemeral=True,
            )
            return

        if self.for_user_id:
            self.bot.db.set_user_threshold(
                self.item.id,
                self.for_user_id,
                target_price_gbp=target_val,
                min_discount_pct=disc_val,
            )
            updated = self.bot.db.get_item(self.item.id)
        else:
            updated = self.bot.db.update_item(
                self.item.id,
                target_price_gbp=target_val,
                min_discount_pct=disc_val,
            )
            if updated and updated.set_number:
                upsert_wishlist_entry(
                    self.bot.config.wishlist_path,
                    {
                        "name": updated.name,
                        "set_number": updated.set_number,
                        "target_price_gbp": updated.target_price_gbp,
                        "min_discount_pct": updated.min_discount_pct,
                    },
                )

        if not updated:
            await interaction.response.send_message(
                embed=make_error_embed("Not Found", "This set is no longer on the wishlist."),
                ephemeral=True,
            )
            return

        if self.detail_view is not None:
            self.detail_view.item = updated
            await interaction.response.edit_message(embed=self.detail_view.build_embed(), view=self.detail_view)
        else:
            await interaction.response.send_message(
                embed=make_status_embed(
                    f"✏️ Updated: {format_item_title(updated)}",
                    (
                        f"• **Target Price:** {f'£{target_val:.2f}' if target_val else 'None'}\n"
                        f"• **Min Discount:** {disc_val:.0f}%"
                    ),
                    color=discord.Color.green(),
                ),
                ephemeral=True,
            )


class SetDetailView(discord.ui.View):
    """Hub detail card and quick actions for a single tracked set (/set)."""

    def __init__(self, bot: "LegoTrackerBot", item: TrackedItem) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.item = item

    def build_embed(self) -> discord.Embed:
        item = self.item
        latest = self.bot.db.get_latest_price(item.id)
        last_dt = self.bot.db.get_last_checked_at(item.id)
        rrp_str = f"£{item.rrp_gbp:.2f}" if item.rrp_gbp > 0 else "Auto-detect"
        target_str = f"£{item.target_price_gbp:.2f}" if item.target_price_gbp else "None"

        if latest:
            if item.rrp_gbp > 0:
                disc_pct = ((item.rrp_gbp - latest.total_gbp) / item.rrp_gbp) * 100
                disc_part = f" ({format_discount_short(disc_pct)})"
            else:
                disc_part = ""
            price_line = f"🏷️ **Latest:** £{latest.total_gbp:.2f}{disc_part} at {latest.retailer}"
        else:
            price_line = "🏷️ **Latest:** *Not checked yet*"

        checked_line = (
            f"🕒 **Last Checked:** <t:{int(last_dt.timestamp())}:R>"
            if last_dt is not None
            else "🕒 **Last Checked:** *Never*"
        )
        cat_line = f"📂 **Category:** {item.category}\n" if item.category else ""
        status_line = "📦 **Status:** Retired (archived)\n" if item.status == "retired" else (
            "⚠️ **Retiring Soon:** Yes\n" if item.is_retiring else ""
        )
        pings = format_pings(self.bot.config.user_id, item, extra_user_ids=self.bot.config.extra_notify_user_ids)

        user_thresh = self.bot.db.get_user_thresholds(item.id)
        thresh_lines = ""
        if user_thresh:
            parts = []
            for uid, (t_gbp, m_pct) in user_thresh.items():
                t_s = f"£{t_gbp:.2f}" if t_gbp else "—"
                d_s = f"≥{m_pct:.0f}%" if m_pct is not None else "—"
                parts.append(f"<@{uid}> ({t_s} / {d_s})")
            thresh_lines = f"\n👤 **Custom Thresholds:** {', '.join(parts)}"

        embed = discord.Embed(
            title=f"🧩 {format_item_title(item)}",
            description=(
                f"{cat_line}"
                f"💷 **Normal RRP:** {rrp_str}\n"
                f"{price_line}\n"
                f"🎯 **Alert:** ≥{item.min_discount_pct:.0f}% off | Target {target_str}\n"
                f"{status_line}"
                f"{checked_line}\n"
                f"🔔 **Notify:** {pings}"
                f"{thresh_lines}"
            ),
            color=discord.Color.dark_grey() if item.status == "retired" else discord.Color.blue(),
        )
        box_art = item.image_url or default_box_art_url(item.set_number)
        if box_art:
            embed.set_thumbnail(url=box_art)
        embed.set_footer(text=f"Set #{item.set_number or item.id}")
        return embed

    @discord.ui.button(label="Re-check", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
    async def recheck_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.item.set_number:
            await interaction.response.send_message("Set number unavailable.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embeds = await self.bot.run_price_checks(set_number=self.item.set_number, force_refresh=True)
        if embeds:
            await interaction.followup.send(embed=embeds[0], ephemeral=True)

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(EditSetModal(self.bot, self.item, detail_view=self))

    @discord.ui.button(label="My Threshold", emoji="🎯", style=discord.ButtonStyle.secondary, row=0)
    async def my_thresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            EditSetModal(self.bot, self.item, detail_view=self, for_user_id=str(interaction.user.id))
        )

    @discord.ui.button(label="History", emoji="📈", style=discord.ButtonStyle.secondary, row=1)
    async def history_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed, file = self.bot._build_history_embed(self.item)
        if file is not None:
            await interaction.response.send_message(embed=embed, file=file, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Pings", emoji="🔔", style=discord.ButtonStyle.secondary, row=1)
    async def pings_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = PingManagerView(self.bot, target_user=interaction.user)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.bot.db.remove_item(self.item.id)
        if self.item.set_number:
            remove_wishlist_entry(self.bot.config.wishlist_path, self.item.set_number)
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            embed=make_status_embed(
                "🗑️ Removed from Wishlist",
                f"Removed **{format_item_title(self.item)}**.",
                color=discord.Color.orange(),
            ),
            view=self,
        )


class _WishlistSelect(discord.ui.Select):
    def __init__(self, items: list[TrackedItem]) -> None:
        options = [
            discord.SelectOption(
                label=format_item_title(item)[:100],
                value=str(item.id),
                description=(
                    "Retired (archived)"
                    if item.status == "retired"
                    else (item.category or f"Set #{item.set_number or item.id}")
                )[:100],
            )
            for item in items[:25]
        ]
        super().__init__(placeholder="Inspect or edit a set...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, WishlistView)
        item_id = int(self.values[0])
        found = next((it for it in view.bot.db.list_items(include_retired=True) if it.id == item_id), None)
        if not found:
            await interaction.response.send_message(
                embed=make_error_embed("Not Found", "That set is no longer on the wishlist."),
                ephemeral=True,
            )
            return
        detail = SetDetailView(view.bot, found)
        await interaction.response.send_message(embed=detail.build_embed(), view=detail, ephemeral=True)


class _WishlistSortSelect(discord.ui.Select):
    def __init__(self, current_sort: str) -> None:
        options = [
            discord.SelectOption(label="Sort: % off (highest first)", value="discount", default=(current_sort == "discount")),
            discord.SelectOption(label="Sort: Price (lowest first)", value="price", default=(current_sort == "price")),
            discord.SelectOption(label="Sort: Name (A–Z)", value="name", default=(current_sort == "name")),
        ]
        super().__init__(placeholder="Sort wishlist...", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, WishlistView)
        view.sort_by = self.values[0]
        view.page = 0
        view._rebuild()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class WishlistView(discord.ui.View):
    """Compact, paginated wishlist display with sorting and quick set inspector."""

    PAGE_SIZE = 10

    def __init__(self, bot: "LegoTrackerBot", page: int = 0, sort_by: str = "discount") -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.page = page
        self.sort_by = sort_by
        self._rebuild()

    def _sorted_items(self) -> list[TrackedItem]:
        items = self.bot.db.list_items(include_retired=True)

        def _key(item: TrackedItem) -> tuple:
            is_ret = 1 if item.status == "retired" else 0
            latest = self.bot.db.get_latest_price(item.id)
            if self.sort_by == "name":
                return (is_ret, format_item_title(item).lower())
            if self.sort_by == "price":
                return (is_ret, latest.total_gbp if latest else 1e9, format_item_title(item).lower())
            # default: discount descending
            if latest and item.rrp_gbp > 0:
                disc = ((item.rrp_gbp - latest.total_gbp) / item.rrp_gbp) * 100
            else:
                disc = -999.0
            return (is_ret, -disc, format_item_title(item).lower())

        return sorted(items, key=_key)

    def _get_pages(self) -> tuple[list[TrackedItem], int, int]:
        items = self._sorted_items()
        total_pages = max(1, (len(items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(self.page, total_pages - 1))
        start = self.page * self.PAGE_SIZE
        return items[start : start + self.PAGE_SIZE], len(items), total_pages

    def _rebuild(self) -> None:
        self.clear_items()
        page_items, _, total_pages = self._get_pages()
        if page_items:
            self.add_item(_WishlistSelect(page_items))
            self.add_item(_WishlistSortSelect(self.sort_by))
        if total_pages > 1:
            prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=(self.page == 0), row=2)
            next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=(self.page >= total_pages - 1), row=2)

            async def _on_prev(interaction: discord.Interaction) -> None:
                self.page -= 1
                self._rebuild()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

            async def _on_next(interaction: discord.Interaction) -> None:
                self.page += 1
                self._rebuild()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

            prev_btn.callback = _on_prev
            next_btn.callback = _on_next
            self.add_item(prev_btn)
            self.add_item(next_btn)

    def build_embed(self) -> discord.Embed:
        page_items, total_count, total_pages = self._get_pages()
        if not total_count:
            return make_status_embed(
                "🧩 Your LEGO Wishlist",
                "Your wishlist is empty. Use `/add <set_number>` to add a set!",
                color=discord.Color.blue(),
            )
        lines: list[str] = []
        for item in page_items:
            title = format_item_title(item)
            if item.status == "retired":
                lines.append(f"📦 ~~{title}~~ — *retired (archived)*")
                continue
            latest = self.bot.db.get_latest_price(item.id)
            last_dt = self.bot.db.get_last_checked_at(item.id)
            retire_tag = " ⚠️" if item.is_retiring else ""
            checked_part = f" · <t:{int(last_dt.timestamp())}:R>" if last_dt else ""
            if latest:
                if item.rrp_gbp > 0:
                    disc_pct = ((item.rrp_gbp - latest.total_gbp) / item.rrp_gbp) * 100
                    disc_str = f" `{format_discount_short(disc_pct)}`"
                else:
                    disc_pct = 0.0
                    disc_str = ""
                hit_target = item.target_price_gbp is not None and latest.total_gbp <= item.target_price_gbp
                hit_disc = disc_pct >= item.min_discount_pct
                dot = "🟢" if (hit_target or hit_disc) else ("🟡" if disc_pct > 0 else "⚪")
                target_tag = " 🎯" if hit_target else ""
                lines.append(f"{dot} **{title}** — **£{latest.total_gbp:.2f}**{disc_str}{target_tag}{retire_tag}{checked_part}")
            else:
                rrp_str = f"RRP £{item.rrp_gbp:.2f}" if item.rrp_gbp > 0 else "RRP auto"
                lines.append(f"⚪ **{title}** — *Not checked* · {rrp_str}{retire_tag}")

        page_tag = f" (Page {self.page + 1}/{total_pages})" if total_pages > 1 else ""
        return make_status_embed(
            f"🧩 Your LEGO Wishlist ({total_count} sets){page_tag}",
            "\n".join(lines),
            color=discord.Color.blue(),
            footer="Pick a set below to view details, edit targets, or re-check",
        )


class RetiredSetView(discord.ui.View):
    """Restore button attached to retired-set archive notifications."""

    def __init__(self, bot: "LegoTrackerBot", item: TrackedItem) -> None:
        super().__init__(timeout=86400)
        self.bot = bot
        self.item = item

    @discord.ui.button(label="Restore Set", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def readd_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        restored = self.bot.db.restore_item(self.item.id)
        if restored is None and self.item.set_number and self.bot.db.get_item_by_set_number(self.item.set_number) is None:
            restored = self.bot.db.add_item(
                name=self.item.name,
                rrp_gbp=self.item.rrp_gbp,
                set_number=self.item.set_number,
                category=self.item.category,
                target_price_gbp=self.item.target_price_gbp,
                min_discount_pct=self.item.min_discount_pct,
                url=self.item.url,
                retailer=self.item.retailer,
                is_retiring=self.item.is_retiring,
                image_url=self.item.image_url,
                notify_user_ids=self.item.notify_user_ids,
            )
        if restored and restored.set_number:
            upsert_wishlist_entry(
                self.bot.config.wishlist_path,
                {
                    "name": restored.name,
                    "set_number": restored.set_number,
                    "category": restored.category,
                    "rrp_gbp": restored.rrp_gbp if restored.rrp_gbp > 0 else None,
                    "target_price_gbp": restored.target_price_gbp,
                    "min_discount_pct": restored.min_discount_pct,
                    "is_retiring": restored.is_retiring,
                    "image_url": restored.image_url,
                    "notify_user_ids": list(restored.notify_user_ids),
                },
            )
        button.disabled = True
        button.label = "Restored"
        await interaction.response.edit_message(view=self)
