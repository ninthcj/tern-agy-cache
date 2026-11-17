--!strict
-- agy-cache: Native prompt-cache bar above every Antigravity (`agy`) composer + widget bar ring & details canvas.

local ui = tern.ui
local probe_path = tern.plugin.dir .. "/probe.py"
local DETAILS_ACTION = "plugin.agy-cache.details"
local TTL_MS = 5 * 60 * 1000 -- Empirical ~5-minute implicit KV/prefix cache TTL in Antigravity
local WARN_MS = 60 * 1000
local NBSP = "\194\160"
local KV_SETTINGS = "settings_v1"

type Settings = {
	composer_bar: boolean,
	expiry_toast: boolean,
}

type TurnStat = {
	step: number,
	model: string,
	input: number,
	cache_read: number,
	output: number,
	hit: number,
	at: string,
}

type PaneCache = {
	pane: number,
	conversation_id: string,
	cwd: string,
	turn_count: number,
	last_epoch_ms: number?,
	last_hit: number,
	total_hit: number,
	last_input: number,
	last_cache_read: number,
	last_output: number,
	total_input: number,
	total_cache_read: number,
	total_output: number,
	model: string,
	series: { number },
	turns: { TurnStat },
}

local settings: Settings = {
	composer_bar = true,
	expiry_toast = false,
}

local by_pane: { [number]: PaneCache } = {}
local toasted_epoch: { [number]: number } = {}
local last_focused_agy: number? = nil
local details_canvas: number? = nil
local refresh_inflight = false
local refresh_queued = false
local installed_line_css = ""
local tick_armed = false

local function load_settings()
	local ok, raw = pcall(tern.kv.get, KV_SETTINGS)
	if ok and type(raw) == "table" then
		if type(raw.composer_bar) == "boolean" then
			settings.composer_bar = raw.composer_bar
		end
		if type(raw.expiry_toast) == "boolean" then
			settings.expiry_toast = raw.expiry_toast
		end
	end
end

local function save_settings()
	pcall(tern.kv.set, KV_SETTINGS, settings)
end

local function now_epoch_ms(): number
	return os.time() * 1000
end

local function fmt_tokens(n: number): string
	if n >= 1e6 then
		return string.format("%.2fM", n / 1e6)
	elseif n >= 1e3 then
		return string.format("%.1fk", n / 1e3)
	end
	return tostring(math.floor(n))
end

local function fmt_pct(v: number): string
	return string.format("%.0f%%", math.max(0, math.min(100, v * 100)))
end

local function fmt_pct1(v: number): string
	return string.format("%.1f%%", math.max(0, math.min(100, v * 100)))
end

local function fmt_clock(sec: number): string
	sec = math.max(0, math.ceil(sec))
	return string.format("%02d:%02d", math.floor(sec / 60), sec % 60)
end

local function cache_stage(entry: PaneCache): (string, number)
	local base_ms = entry.last_epoch_ms or 0
	if base_ms <= 0 then
		return "warm", TTL_MS
	end
	local left_ms = (base_ms + TTL_MS) - now_epoch_ms()
	if left_ms <= 0 then
		return "cold", left_ms
	elseif left_ms <= WARN_MS then
		return "expiring", left_ms
	end
	return "warm", left_ms
end

local function hit_tone(v: number, turn_count: number): string?
	if turn_count == 0 then
		return "muted"
	end
	if v >= 0.75 then
		return "success"
	elseif v >= 0.40 then
		return "warning"
	end
	return "error"
end

-- Tern requires relative `rgb(from var(...) r g b)` inside `linear-gradient(...)` when vars hold `light-dark()`.
local function solid(color: string): string
	return "rgb(from " .. color .. " r g b)"
end

local function faded(color: string, pct: number): string
	return string.format("rgb(from %s r g b / %d%%)", color, pct)
end

local function em(glyphs: number): string
	return string.format("%.3fem", glyphs * 0.6)
end

local function glyph_count(s: string): number
	local _, n = string.gsub(s, "[^\128-\191]", "")
	return n
end

type PaintedLine = {
	text: string,
	paint: string,
}

local function build_segmented_line(segs: { { text: string, color: string } }): PaintedLine
	local parts: { string } = {}
	local stops: { string } = {}
	local cursor = 0
	for _, seg in ipairs(segs) do
		local t = (string.gsub(seg.text, " ", NBSP))
		local n = glyph_count(t)
		if n > 0 then
			table.insert(parts, t)
			table.insert(stops, string.format("%s %s %s", seg.color, em(cursor), em(cursor + n)))
			cursor += n
		end
	end
	return {
		text = table.concat(parts),
		paint = "linear-gradient(90deg, " .. table.concat(stops, ", ") .. ")",
	}
end

local function build_pane_rule(stage_index: number, entry: PaneCache): string
	local st, left_ms = cache_stage(entry)
	local ratio = math.max(0, math.min(1, entry.last_hit))

	local stage_label = if st ~= "cold" and entry.last_cache_read == 0 and entry.last_input > 0
		then "primed"
		else st
	local stage_col = if st == "cold"
		then solid("var(--bad)")
		elseif st == "expiring" or stage_label == "primed" then solid("var(--warn)")
		else solid("var(--ok)")

	local hit_col = if ratio >= 0.75
		then solid("var(--ok)")
		elseif ratio >= 0.40 then solid("var(--warn)")
		else solid("var(--bad)")

	local t2 = solid("var(--t2)")
	local t3 = solid("var(--t3)")
	local t4 = solid("var(--t4)")
	local track = faded("var(--t1)", 15)

	-- Left half (`::before`): `⛁ cache  warm  read 123.5k · uncached 9.9k · avg 83.7%`
	local left_line = build_segmented_line({
		{ text = "⛁ cache  ", color = t2 },
		{ text = stage_label .. "  ", color = stage_col },
		{ text = "read ", color = t3 },
		{ text = fmt_tokens(entry.last_cache_read), color = solid("var(--ok)") },
		{ text = " · ", color = t4 },
		{ text = "uncached ", color = t3 },
		{ text = fmt_tokens(entry.last_input), color = solid("var(--warn)") },
		{ text = " · ", color = t4 },
		{ text = "avg " .. fmt_pct1(entry.total_hit), color = t3 },
	})

	-- Right half (`::after`, flush-right above `1M`): `hit rate  ━━━━━━━━  98.1% | 04:32`
	local r_prefix = (string.gsub("hit rate  ", " ", NBSP))
	local bar_glyphs = 8
	local bar_str = string.rep("━", bar_glyphs)
	local r_mid = NBSP .. NBSP .. fmt_pct1(ratio)
	local r_sep = NBSP .. "|" .. NBSP
	local r_tail = if st == "cold" then "cold" else fmt_clock(left_ms / 1000)

	local p_len = glyph_count(r_prefix)
	local bar_fill = ratio * bar_glyphs
	local bar_end = p_len + bar_glyphs
	local mid_end = bar_end + glyph_count(r_mid)
	local sep_end = mid_end + glyph_count(r_sep)
	local tail_end = sep_end + glyph_count(r_tail)

	local r_stops: { string } = {
		string.format("%s %s %s", t3, em(0), em(p_len)),
	}
	if bar_fill > 0.001 then
		table.insert(r_stops, string.format("%s %s %s", hit_col, em(p_len), em(p_len + bar_fill)))
	end
	if bar_fill < bar_glyphs - 0.001 then
		table.insert(r_stops, string.format("%s %s %s", track, em(p_len + bar_fill), em(bar_end)))
	end
	table.insert(r_stops, string.format("%s %s %s", hit_col, em(bar_end), em(mid_end)))
	table.insert(r_stops, string.format("%s %s %s", t4, em(mid_end), em(sep_end)))
	table.insert(r_stops, string.format("%s %s %s", stage_col, em(sep_end), em(tail_end)))

	local right_text = r_prefix .. bar_str .. r_mid .. r_sep .. r_tail
	local right_paint = "linear-gradient(90deg, " .. table.concat(r_stops, ", ") .. ")"

	local pane_sel = string.format("div.tn-stage > section.tn-pane:nth-of-type(%d)", stage_index)
	local editor_sel = pane_sel .. " .sf-dock .sf-col[data-role^='omp.editor']"
	local meter_sel = pane_sel .. " .sf-dock .sf-meter[data-role='omp.composer.context']"

	return string.format(
		[[
%s {
	margin-top: 28px;
}
%s::before,
%s::after {
	position: absolute;
	bottom: 100%%;
	margin-bottom: 6px;
	height: 15px;
	overflow: hidden;
	white-space: nowrap;
	font: 500 11px/15px var(--mono);
	font-variant-numeric: tabular-nums;
	color: transparent;
	background-repeat: no-repeat;
	background-clip: text;
	pointer-events: none;
}
%s::before {
	content: "%s";
	left: -8px;
	right: 240px;
	text-overflow: ellipsis;
	background-image: %s;
}
%s::after {
	content: "%s";
	right: -8px;
	width: %.3fem;
	background-image: %s;
}
]],
		editor_sel,
		meter_sel,
		meter_sel,
		meter_sel,
		left_line.text,
		left_line.paint,
		meter_sel,
		right_text,
		tail_end * 0.6,
		right_paint
	)
end

local function check_expiry_toasts(cx: WindowCx)
	if not settings.expiry_toast then
		return
	end
	local focused = cx.session:focused()
	for pane_id, entry in pairs(by_pane) do
		local epoch = entry.last_epoch_ms or 0
		if epoch > 0 and entry.turn_count > 0 and pane_id ~= focused then
			local st, left_ms = cache_stage(entry)
			if st == "expiring" and toasted_epoch[pane_id] ~= epoch then
				toasted_epoch[pane_id] = epoch
				local stake = entry.last_cache_read + entry.last_input
				cx:toast(
					"info",
					string.format("Antigravity cache expiring (%s)", fmt_clock(left_ms / 1000)),
					string.format("%s prefix in %s", fmt_tokens(stake), entry.cwd ~= "" and entry.cwd or string.sub(entry.conversation_id, 1, 8))
				)
			end
		end
	end
end

local function update_composer_css(cx: WindowCx?)
	if not cx then
		return
	end
	if not settings.composer_bar then
		if installed_line_css ~= "" then
			tern.css("composer-line", "")
			installed_line_css = ""
		end
		return
	end

	local cur_tabs: { [number]: boolean }? = nil
	for _, s in ipairs(cx.session:sessions()) do
		if s.current then
			cur_tabs = {}
			for _, tid in ipairs(s.tabs) do
				(cur_tabs :: { [number]: boolean })[tid] = true
			end
			break
		end
	end

	local rules: { string } = {}
	local idx = 0
	for _, p in ipairs(cx.session:panes()) do
		if p.tab ~= nil and (cur_tabs == nil or cur_tabs[p.tab]) then
			idx += 1
			local entry = by_pane[p.pane]
			if entry and entry.turn_count > 0 then
				table.insert(rules, build_pane_rule(idx, entry))
			end
		end
	end

	local css = table.concat(rules, "\n")
	if css ~= installed_line_css then
		tern.css("composer-line", css)
		installed_line_css = css
	end
end

local ensure_tick: () -> ()

local function on_tick(cx: WindowCx?)
	tick_armed = false
	if next(by_pane) == nil then
		return
	end
	if cx then
		update_composer_css(cx)
		check_expiry_toasts(cx)
	end
	ensure_tick()
end

ensure_tick = function()
	if not tick_armed and next(by_pane) ~= nil then
		tick_armed = true
		tern.timer(1000, on_tick)
	end
end

local function is_canvas_open(cx: WindowCx, id: number?): boolean
	return id ~= nil and cx.canvas:get(id) ~= nil
end

local function build_canvas_view(cx: WindowCx): Node
	local focused = cx.session:focused()
	local active_id = if focused and by_pane[focused] then focused else last_focused_agy

	local panes_list: { PaneCache } = {}
	for _, entry in pairs(by_pane) do
		table.insert(panes_list, entry)
	end
	table.sort(panes_list, function(a, b)
		if a.pane == active_id and b.pane ~= active_id then
			return true
		end
		if b.pane == active_id and a.pane ~= active_id then
			return false
		end
		return a.pane < b.pane
	end)

	if #panes_list == 0 then
		return ui.col({
			ui.text(ui.span("No active Antigravity (agy) ACP panes detected.", "muted")),
		})
	end

	local children: { Node } = {}
	for _, entry in ipairs(panes_list) do
		local is_active = (entry.pane == active_id)
		local st, left_ms = cache_stage(entry)
		local ttl_text = if st == "cold" then "cold (>5m idle)" else (st .. " · " .. fmt_clock(left_ms / 1000) .. " left")
		local head_text = string.format(
			"agy · %s (last %s · avg %s)",
			string.format("%s", string.sub(entry.conversation_id, 1, 8)),
			fmt_pct1(entry.last_hit),
			fmt_pct1(entry.total_hit)
		)
		local summary = ui.kv({
			{ { ui.span("Conversation", "muted") }, { ui.span(entry.conversation_id, "strong") } },
			{ { ui.span("Model", "muted") }, { entry.model ~= "" and entry.model or "—" } },
			{ { ui.span("5m TTL window", "muted") }, { ttl_text } },
			{
				{ ui.span("Latest turn", "muted") },
				{
					string.format(
						"%s hit (%s cached / %s uncached / %s out)",
						fmt_pct1(entry.last_hit),
						fmt_tokens(entry.last_cache_read),
						fmt_tokens(entry.last_input),
						fmt_tokens(entry.last_output)
					),
				},
			},
			{
				{ ui.span("Session total", "muted") },
				{
					string.format(
						"%s hit across %d turns (%s cached / %s uncached)",
						fmt_pct1(entry.total_hit),
						entry.turn_count,
						fmt_tokens(entry.total_cache_read),
						fmt_tokens(entry.total_input)
					),
				},
			},
		})

		local card_nodes: { Node } = { summary }
		if is_active and #entry.turns > 0 then
			local rows: { { Cell } } = {}
			for i = #entry.turns, 1, -1 do
				local t = entry.turns[i]
				table.insert(rows, {
					"#" .. tostring(t.step),
					fmt_pct1(t.hit),
					fmt_tokens(t.cache_read),
					fmt_tokens(t.input),
					fmt_tokens(t.output),
				})
			end
			table.insert(
				card_nodes,
				ui.table(
					{
						{ head = "Step" },
						{ head = "Hit" },
						{ head = "Cached" },
						{ head = "Uncached" },
						{ head = "Out" },
					},
					rows
				)
			)
		end

		table.insert(
			children,
			ui.card(
				{ ui.span(head_text, if is_active then "strong" else nil) },
				card_nodes,
				{ icon = "gauge", meta = if entry.cwd ~= "" then entry.cwd else nil }
			)
		)
	end

	return ui.col(children)
end

local function refresh_canvas(cx: WindowCx)
	if is_canvas_open(cx, details_canvas) then
		cx.canvas:set(details_canvas :: number, { view = build_canvas_view(cx) })
	end
end

local function run_probe()
	if refresh_inflight then
		refresh_queued = true
		return
	end
	refresh_inflight = true
	tern.process.run({ "python3", probe_path }, { timeout_ms = 3000 }, function(res: ProcessResult, later: WindowCx?)
		refresh_inflight = false
		if res.status == 0 and res.stdout ~= "" then
			local ok_j, decoded = pcall(tern.json.decode, res.stdout)
			if ok_j and type(decoded) == "table" and type(decoded.panes) == "table" then
				local next_map: { [number]: PaneCache } = {}
				for k, v in pairs(decoded.panes) do
					local pid = tonumber(k)
					if pid and type(v) == "table" then
						next_map[pid] = v :: PaneCache
					end
				end
				by_pane = next_map
				update_composer_css(later)
				ensure_tick()
				tern.chrome.refresh()
				if later then
					refresh_canvas(later)
				end
			end
		end
		if refresh_queued then
			refresh_queued = false
			run_probe()
		end
	end)
end

tern.command({
	id = "details",
	title = "Antigravity Cache: Show details",
	icon = "gauge",
	run = function(cx: WindowCx)
		run_probe()
		if is_canvas_open(cx, details_canvas) then
			cx.canvas:set(details_canvas :: number, { view = build_canvas_view(cx) })
			cx.layout:focus(details_canvas :: number)
			return
		end
		details_canvas = cx.canvas:open({
			title = "Antigravity Cache",
			view = build_canvas_view(cx),
			about = "Prompt-cache hit rate per turn and session total for live Antigravity (agy) panes.",
			how = "beside",
		})
	end,
})

tern.command({
	id = "toggle_bar",
	title = "Antigravity Cache: Toggle composer bar",
	icon = "gauge",
	run = function(cx: WindowCx)
		settings.composer_bar = not settings.composer_bar
		save_settings()
		update_composer_css(cx)
		cx:toast("info", "Antigravity composer bar " .. (if settings.composer_bar then "enabled" else "hidden"))
	end,
})

tern.command({
	id = "toggle_toast",
	title = "Antigravity Cache: Toggle 1-minute expiry toast",
	icon = "bell",
	run = function(cx: WindowCx)
		settings.expiry_toast = not settings.expiry_toast
		save_settings()
		cx:toast("info", "Antigravity cache expiry toast " .. (if settings.expiry_toast then "enabled" else "disabled"))
	end,
})

local function build_view(pane: StatusPaneInfo?): WidgetView?
	local target: PaneCache? = nil
	if pane and by_pane[pane.pane] then
		target = by_pane[pane.pane]
		last_focused_agy = pane.pane
	elseif last_focused_agy and by_pane[last_focused_agy] then
		target = by_pane[last_focused_agy]
	else
		for _, entry in pairs(by_pane) do
			target = entry
			break
		end
	end
	if not target then
		return nil
	end

	local label = if target.turn_count == 0
		then "agy —"
		else string.format("agy %s (avg %s)", fmt_pct(target.last_hit), fmt_pct(target.total_hit))

	local tip = string.format(
		"Antigravity %s · Latest: %s (%s cached / %s uncached) · Total: %s across %d turns",
		string.sub(target.conversation_id, 1, 8),
		fmt_pct1(target.last_hit),
		fmt_tokens(target.last_cache_read),
		fmt_tokens(target.last_input),
		fmt_pct1(target.total_hit),
		target.turn_count
	)

	return {
		text = label,
		icon = "gauge",
		tone = hit_tone(target.last_hit, target.turn_count),
		title = tip,
		command = DETAILS_ACTION,
		value = target.last_hit,
		series = target.series,
	}
end

tern.chrome.widget("hit", {
	name = "Antigravity Cache",
	description = "Prompt-cache hit rate (latest turn & session average) for live agy panes.",
	icon = "gauge",
	styles = { "ring", "bar", "text", "graph" },
	render = build_view,
})

tern.on("focus", function(ev: FocusEvent, cx: WindowCx)
	if by_pane[ev.pane] then
		last_focused_agy = ev.pane
		tern.chrome.refresh()
	end
	update_composer_css(cx)
	run_probe()
end)

tern.on("agent_finished", function()
	run_probe()
end)

tern.on("agent_state", function()
	run_probe()
end)

tern.on("pane_created", function(_ev: PaneEvent, cx: WindowCx)
	update_composer_css(cx)
	run_probe()
end)

tern.on("pane_closed", function(ev: PaneEvent, cx: WindowCx)
	toasted_epoch[ev.pane] = nil
	if by_pane[ev.pane] then
		by_pane[ev.pane] = nil
		if last_focused_agy == ev.pane then
			last_focused_agy = nil
		end
		tern.chrome.refresh()
	end
	update_composer_css(cx)
end)

tern.on("tab_created", function(_ev: TabEvent, cx: WindowCx)
	update_composer_css(cx)
end)

tern.on("tab_closed", function(_ev: TabEvent, cx: WindowCx)
	update_composer_css(cx)
end)

tern.on("window_start", function(cx: WindowCx)
	load_settings()
	update_composer_css(cx)
	run_probe()
end)

load_settings()
tern.timer(0, run_probe)
