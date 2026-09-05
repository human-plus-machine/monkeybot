/**
 * @vendored-from page-agent packages/page-controller/src/dom/index.ts, actions.ts
 *
 * Driver glue injected alongside dom_tree.js (window.__bmcpBuildDomTree). Ported
 * from PageController's flatTreeToString/getSelectorMap/click/input logic, trimmed
 * to what browser-mcp needs: build an indexed text tree once, then act on indices
 * via CDP-driven clicks/typing from the Python side, plus in-page fill/settle.
 *
 * Exposes window.__bmcp = { getTree, getRect, getRects, getInputInfo, selectOption, fill, settle }
 *
 * Indices are stable for the lifetime of the document (never recycled). Call
 * getTree again after navigation; the same element keeps its index across
 * in-document mutations.
 */
;(function () {
	const DEFAULT_INCLUDE_ATTRIBUTES = [
		'title', 'type', 'checked', 'name', 'role', 'value', 'placeholder',
		'data-date-format', 'alt', 'aria-label', 'aria-expanded', 'data-state',
		'aria-checked', 'id', 'for', 'target', 'aria-haspopup', 'aria-controls',
		'aria-owns', 'contenteditable',
	]
	const KIND_ALL = 'all'
	const KIND_INPUTS = 'inputs'
	const KIND_BUTTONS = 'buttons'
	const KIND_LINKS = 'links'

	if (!window.__bmcpIndexByKey) {
		window.__bmcpIndexByKey = Object.create(null)
		window.__bmcpNextIndex = 0
		window.__bmcpElToIndex = new WeakMap()
		window.__bmcpIndexToEl = new Map()
	}

	function capText(text, maxLength) {
		return text.length > maxLength ? text.substring(0, maxLength) + '...' : text
	}

	function collapseWs(text) {
		return String(text || '').replace(/\s+/g, ' ').trim()
	}

	function hrefPath(href) {
		if (!href) return ''
		try {
			const u = new URL(href, location.href)
			return u.pathname + (u.search || '') + (u.hash || '')
		} catch (e) {
			return String(href)
		}
	}

	function matchAttributes(attrs, patterns) {
		const result = {}
		for (const key of Object.keys(attrs)) {
			const value = attrs[key]
			if (patterns.includes(key) && value && value.trim()) result[key] = value.trim()
		}
		return result
	}

	function nthOfType(el) {
		const tag = el.tagName
		let n = 1
		let sib = el.previousElementSibling
		while (sib) {
			if (sib.tagName === tag) n += 1
			sib = sib.previousElementSibling
		}
		return n
	}

	function ancestorPath(el) {
		const parts = []
		let cur = el.parentElement
		for (let i = 0; i < 3 && cur; i++) {
			parts.push(cur.tagName.toLowerCase() + ':' + nthOfType(cur))
			cur = cur.parentElement
		}
		return parts.join('/')
	}

	function ownText(el) {
		let text = ''
		const kids = el.childNodes || []
		for (let i = 0; i < kids.length; i++) {
			if (kids[i].nodeType === 3) text += kids[i].textContent || ''
		}
		return collapseWs(text).slice(0, 40)
	}

	function attrOf(el, name) {
		try {
			return el.getAttribute(name) || ''
		} catch (e) {
			return ''
		}
	}

	function stableKey(el) {
		const tag = (el.tagName || '').toLowerCase()
		const parts = [
			tag,
			el.id || '',
			attrOf(el, 'name'),
			attrOf(el, 'role'),
			attrOf(el, 'aria-label'),
			attrOf(el, 'placeholder'),
			attrOf(el, 'type'),
			hrefPath(attrOf(el, 'href')),
			ownText(el),
			ancestorPath(el),
		]
		return parts.join('|')
	}

	function derefIndex(index) {
		const ref = window.__bmcpIndexToEl.get(index)
		if (!ref) return null
		return typeof ref.deref === 'function' ? ref.deref() : ref
	}

	function ownerAlive(index) {
		const el = derefIndex(index)
		return !!(el && el.isConnected)
	}

	function rememberIndex(el, index) {
		window.__bmcpElToIndex.set(el, index)
		if (typeof WeakRef === 'function') {
			window.__bmcpIndexToEl.set(index, new WeakRef(el))
		} else {
			window.__bmcpIndexToEl.set(index, el)
		}
	}

	function assignStableIndex(el) {
		if (!el) return window.__bmcpNextIndex++
		const existing = window.__bmcpElToIndex.get(el)
		if (existing !== undefined && el.isConnected) return existing
		const key = stableKey(el)
		const byKey = window.__bmcpIndexByKey[key]
		if (byKey !== undefined && !ownerAlive(byKey)) {
			rememberIndex(el, byKey)
			return byKey
		}
		const index = window.__bmcpNextIndex++
		rememberIndex(el, index)
		if (byKey === undefined) window.__bmcpIndexByKey[key] = index
		return index
	}

	function inViewport(el) {
		if (!el || typeof el.getBoundingClientRect !== 'function') return false
		const r = el.getBoundingClientRect()
		return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight &&
			r.right > 0 && r.left < window.innerWidth
	}

	function isBelowViewport(el) {
		if (!el || typeof el.getBoundingClientRect !== 'function') return false
		return el.getBoundingClientRect().top >= window.innerHeight
	}

	function matchesKind(node, kind) {
		if (!kind || kind === KIND_ALL) return true
		const tag = String(node.tagName || '').toLowerCase()
		const attrs = node.attributes || {}
		const role = String(attrs.role || '').toLowerCase()
		const type = String(attrs.type || '').toLowerCase()
		const editable = !!(node.ref && node.ref.isContentEditable)
		if (kind === KIND_INPUTS) {
			if (tag === 'textarea' || tag === 'select' || editable) return true
			if (role === 'textbox' || role === 'searchbox' || role === 'combobox') return true
			if (tag === 'input' && type !== 'button' && type !== 'submit' && type !== 'reset') return true
			return false
		}
		if (kind === KIND_BUTTONS) {
			if (tag === 'button' || role === 'button') return true
			if (tag === 'input' && (type === 'submit' || type === 'button' || type === 'reset')) return true
			return false
		}
		if (kind === KIND_LINKS) {
			return tag === 'a' || role === 'link'
		}
		return true
	}

	function buildTreeNode(flatTree, nodeId) {
		const node = flatTree.map[nodeId]
		if (!node) return null
		if (node.type === 'TEXT_NODE') {
			return { type: 'text', text: node.text, isVisible: node.isVisible, parent: null, children: [] }
		}
		const children = (node.children || [])
			.map((childId) => buildTreeNode(flatTree, childId))
			.filter(Boolean)
		return {
			type: 'element',
			tagName: node.tagName,
			attributes: node.attributes || {},
			isVisible: node.isVisible || false,
			isInteractive: node.isInteractive || false,
			isTopElement: node.isTopElement || false,
			highlightIndex: node.highlightIndex,
			ref: node.ref,
			parent: null,
			children,
		}
	}

	function setParents(node, parent) {
		node.parent = parent
		for (const child of node.children) setParents(child, node)
	}

	function hasParentWithHighlight(node) {
		let cur = node.parent
		while (cur) {
			if (cur.type === 'element' && cur.highlightIndex !== undefined) return true
			cur = cur.parent
		}
		return false
	}

	function collectText(node, out) {
		if (node.type === 'text' && node.text) {
			out.push(node.text)
		} else if (node.type === 'element') {
			for (const child of node.children) {
				if (child.type === 'element' && child.highlightIndex !== undefined) continue
				collectText(child, out)
			}
		}
	}

	function textOf(node) {
		const parts = []
		collectText(node, parts)
		return collapseWs(parts.join(' '))
	}

	function formatInteractiveLine(node, depth) {
		const indent = '\t'.repeat(depth)
		let text = textOf(node)
		const attrsToInclude = matchAttributes(node.attributes, DEFAULT_INCLUDE_ATTRIBUTES)
		if (attrsToInclude.role === node.tagName) delete attrsToInclude.role
		for (const attr of ['aria-label', 'placeholder', 'title']) {
			if (attrsToInclude[attr] && attrsToInclude[attr].toLowerCase().trim() === text.toLowerCase()) {
				delete attrsToInclude[attr]
			}
		}
		if (attrsToInclude['aria-label'] || attrsToInclude.placeholder || text) {
			delete attrsToInclude.id
			delete attrsToInclude.name
		}
		const rawHref = (node.attributes && node.attributes.href) || ''
		if (rawHref) attrsToInclude.href = hrefPath(rawHref)
		const attrStr = Object.entries(attrsToInclude)
			.map(([k, v]) => `${k}=${capText(v, 20)}`)
			.join(' ')
		if (text) text = capText(text, 60)
		let line = `${indent}[${node.highlightIndex}]<${node.tagName}`
		if (attrStr) line += ` ${attrStr}`
		if (text) {
			line += (attrStr ? '' : ' ') + `>${text}`
		} else if (!attrStr) {
			line += ' '
		}
		line += ' />'
		return line
	}

	function remapHighlightIndices(node) {
		if (node.type === 'element') {
			if (node.highlightIndex !== undefined && node.ref) {
				node.highlightIndex = assignStableIndex(node.ref)
			}
			for (const child of node.children) remapHighlightIndices(child)
		}
	}

	function flatTreeToString(flatTree, opts) {
		const viewportOnly = !!(opts && opts.viewportOnly)
		const kind = (opts && opts.kind) || null
		const contains = collapseWs((opts && opts.contains) || '').toLowerCase()
		const maxElements = (opts && opts.maxElements != null) ? Math.max(1, opts.maxElements | 0) : 150
		const empty = { text: '', selectorMap: {}, truncated: false, below_viewport: 0, omitted: 0 }
		const root = buildTreeNode(flatTree, flatTree.rootId)
		if (!root) return empty
		setParents(root, null)
		remapHighlightIndices(root)

		const selectorMap = {}
		const items = []

		function parentInViewport(node) {
			let cur = node.parent
			while (cur) {
				if (cur.ref) return inViewport(cur.ref)
				cur = cur.parent
			}
			return true
		}

		function process(node, depth) {
			let nextDepth = depth
			const indent = '\t'.repeat(depth)
			if (node.type === 'element') {
				if (node.highlightIndex !== undefined) {
					nextDepth += 1
					if (node.ref) selectorMap[node.highlightIndex] = node.ref
					items.push({
						line: formatInteractiveLine(node, depth),
						interactive: true,
						inViewport: inViewport(node.ref),
						below: isBelowViewport(node.ref),
						kindMatch: matchesKind(node, kind),
					})
				}
				for (const child of node.children) process(child, nextDepth)
			} else if (node.type === 'text') {
				if (hasParentWithHighlight(node)) return
				if (node.parent && node.parent.type === 'element' && node.parent.isVisible && node.parent.isTopElement) {
					items.push({
						line: `${indent}${node.text || ''}`,
						interactive: false,
						inViewport: parentInViewport(node),
						below: false,
						kindMatch: true,
					})
				}
			}
		}

		process(root, 0)

		let belowViewport = 0
		for (const item of items) {
			if (item.interactive && item.below) belowViewport += 1
		}

		const filtered = []
		for (const item of items) {
			if (item.interactive && !item.kindMatch) continue
			if (contains && item.line.toLowerCase().indexOf(contains) === -1) continue
			if (viewportOnly && !contains && !item.inViewport) continue
			filtered.push(item)
		}

		const shown = []
		let interactiveShown = 0
		let omitted = 0
		for (const item of filtered) {
			if (item.interactive) {
				if (interactiveShown >= maxElements) {
					omitted += 1
					continue
				}
				interactiveShown += 1
			} else if (interactiveShown >= maxElements) {
				continue
			}
			shown.push(item.line)
		}

		return {
			text: shown.join('\n'),
			selectorMap,
			truncated: omitted > 0,
			below_viewport: belowViewport,
			omitted,
		}
	}

	function getTree(opts) {
		let viewportOnly = false
		let kind = null
		let contains = null
		let maxElements = 150
		if (typeof opts === 'boolean') {
			viewportOnly = opts
		} else if (opts && typeof opts === 'object') {
			viewportOnly = !!opts.viewportOnly
			kind = opts.kind || null
			contains = opts.contains || null
			if (opts.maxElements != null) maxElements = opts.maxElements
		}
		const flatTree = window.__bmcpBuildDomTree({
			doHighlightElements: false,
			focusHighlightIndex: -1,
			viewportExpansion: -1,
			debugMode: false,
			interactiveBlacklist: [],
			interactiveWhitelist: [],
			highlightOpacity: 0,
			highlightLabelOpacity: 0,
		})
		const { text, selectorMap, truncated, below_viewport, omitted } = flatTreeToString(flatTree, {
			viewportOnly,
			kind,
			contains,
			maxElements,
		})
		const prev = window.__bmcpSelectorMap || {}
		const merged = {}
		for (const key of Object.keys(prev)) {
			const el = prev[key]
			if (el && el.isConnected) merged[key] = el
		}
		for (const key of Object.keys(selectorMap)) merged[key] = selectorMap[key]
		window.__bmcpSelectorMap = merged
		return {
			url: window.location.href,
			title: document.title,
			elementCount: Object.keys(merged).length,
			tree: text,
			truncated,
			below_viewport,
			omitted,
		}
	}

	function elementByIndex(index) {
		const map = window.__bmcpSelectorMap || {}
		const el = map[index]
		if (!el) throw new Error(`No element at index ${index}. Indices remain valid until navigation; call getTree() after navigation.`)
		return el
	}

	function getRect(index) {
		const el = elementByIndex(index)
		el.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' })
		const rect = el.getBoundingClientRect()
		const x = rect.left + rect.width / 2
		const y = rect.top + rect.height / 2
		const top = document.elementFromPoint(x, y)
		const covered = top && top !== el && !el.contains(top)
		return {
			x,
			y,
			width: rect.width,
			height: rect.height,
			tagName: el.tagName.toLowerCase(),
			visible: rect.width > 0 && rect.height > 0,
			obscuredBy: covered ? top.tagName.toLowerCase() : null,
		}
	}

	function getRects(indices, opts) {
		const options = opts || {}
		const doScroll = !!options.scroll
		const full = !!options.full
		const map = window.__bmcpSelectorMap || {}
		const keys = indices == null ? Object.keys(map).map(Number) : indices
		const vw = window.innerWidth
		const vh = window.innerHeight
		const scrollX = window.scrollX || window.pageXOffset || 0
		const scrollY = window.scrollY || window.pageYOffset || 0
		const out = {}
		for (const i of keys) {
			const el = map[i]
			if (!el) continue
			try {
				if (doScroll) {
					el.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' })
				}
				const rect = el.getBoundingClientRect()
				if (rect.width <= 0 || rect.height <= 0) continue
				if (!doScroll && !full) {
					if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= vh || rect.left >= vw) continue
				}
				out[i] = {
					x: full ? rect.left + scrollX : rect.left,
					y: full ? rect.top + scrollY : rect.top,
					width: rect.width,
					height: rect.height,
					tagName: el.tagName.toLowerCase(),
				}
			} catch (e) { /* skip missing indices */ }
		}
		let cssWidth = vw
		let cssHeight = vh
		if (full) {
			const doc = document.documentElement
			const body = document.body
			cssWidth = Math.max(doc.scrollWidth, body ? body.scrollWidth : 0, vw)
			cssHeight = Math.max(doc.scrollHeight, body ? body.scrollHeight : 0, vh)
		}
		return {
			rects: out,
			cssWidth,
			cssHeight,
			dpr: window.devicePixelRatio || 1,
		}
	}

	/**
	 * Tag the element with a temporary data attribute so the Python side can address it
	 * via a CSS selector (needed for browser-harness's fill_input, which drives real key
	 * events through framework-controlled inputs rather than the index-only ref we keep
	 * in-page).
	 */
	function getInputInfo(index) {
		const el = elementByIndex(index)
		const tag = el.tagName.toLowerCase()
		const isTextLike = tag === 'input' || tag === 'textarea' || el.isContentEditable
		if (!isTextLike) throw new Error(`Element at index ${index} (${tag}) is not a text input`)
		el.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' })
		el.setAttribute('data-bmcp-idx', String(index))
		return { tagName: tag, selector: `[data-bmcp-idx="${index}"]` }
	}

	function selectOption(index, optionText) {
		const el = elementByIndex(index)
		if (el.tagName.toLowerCase() !== 'select') {
			throw new Error(`Element at index ${index} is not a <select>`)
		}
		const options = Array.from(el.options)
		const option = options.find((o) => (o.textContent || '').trim() === optionText.trim())
		if (!option) throw new Error(`Option "${optionText}" not found`)
		el.value = option.value
		el.dispatchEvent(new Event('change', { bubbles: true }))
		return true
	}

	function needsKeyEvents(el) {
		if (el.hasAttribute('data-bmcp-keys')) return true
		if ((el.getAttribute('role') || '').toLowerCase() === 'combobox') return true
		if (el.getAttribute('aria-autocomplete')) return true
		return false
	}

	function setNativeValue(el, value) {
		const tag = el.tagName.toLowerCase()
		const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
		const desc = Object.getOwnPropertyDescriptor(proto, 'value')
		if (desc && desc.set) desc.set.call(el, value)
		else el.value = value
	}

	function fill(index, text, opts) {
		const el = elementByIndex(index)
		const tag = el.tagName.toLowerCase()
		const clear = !opts || opts.clear !== false
		const isTextLike = tag === 'input' || tag === 'textarea' || el.isContentEditable
		if (!isTextLike) throw new Error(`Element at index ${index} (${tag}) is not a text input`)
		el.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' })
		el.focus()
		if (el.isContentEditable) {
			if (clear) {
				const sel = window.getSelection()
				const range = document.createRange()
				range.selectNodeContents(el)
				sel.removeAllRanges()
				sel.addRange(range)
			}
			document.execCommand('insertText', false, text)
		} else {
			const next = clear ? text : String(el.value || '') + text
			setNativeValue(el, next)
			el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }))
			el.dispatchEvent(new Event('change', { bubbles: true }))
		}
		return {
			ok: true,
			value: el.value != null && !el.isContentEditable ? el.value : (el.textContent || ''),
			tagName: tag,
			needsKeys: needsKeyEvents(el),
		}
	}

	let lastMutation = performance.now()
	let mutationCount = 0
	if (!window.__bmcpSettleObserver) {
		const observer = new MutationObserver(function () {
			lastMutation = performance.now()
			mutationCount += 1
		})
		observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true })
		window.__bmcpSettleObserver = observer
	}

	function settle(quietMs, maxMs) {
		const quiet = quietMs == null ? 150 : quietMs
		const max = maxMs == null ? 1500 : maxMs
		const start = performance.now()
		lastMutation = start
		const baseline = mutationCount
		return new Promise(function (resolve) {
			let done = false
			function finish(isQuiet) {
				if (done) return
				done = true
				resolve({ quiet: isQuiet, mutations: mutationCount - baseline })
			}
			function check() {
				if (done) return
				const now = performance.now()
				if (now - lastMutation >= quiet) {
					finish(true)
					return
				}
				if (now - start >= max) {
					finish(false)
					return
				}
				setTimeout(check, 50)
			}
			setTimeout(check, quiet > 0 ? Math.min(quiet, 50) : 0)
		})
	}

	window.__bmcp = { getTree, getRect, getRects, getInputInfo, selectOption, fill, settle }
})();
