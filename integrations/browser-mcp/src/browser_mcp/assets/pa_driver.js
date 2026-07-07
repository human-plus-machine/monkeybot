/**
 * @vendored-from page-agent packages/page-controller/src/dom/index.ts, actions.ts
 *
 * Driver glue injected alongside dom_tree.js (window.__bmcpBuildDomTree). Ported
 * from PageController's flatTreeToString/getSelectorMap/click/input logic, trimmed
 * to what browser-mcp needs: build an indexed text tree once, then act on indices
 * via CDP-driven clicks/typing from the Python side (no in-page event simulation).
 *
 * Exposes window.__bmcp = { getTree, getRect, getInputInfo, selectOption }
 */
;(function () {
	const DEFAULT_INCLUDE_ATTRIBUTES = [
		'title', 'type', 'checked', 'name', 'role', 'value', 'placeholder',
		'data-date-format', 'alt', 'aria-label', 'aria-expanded', 'data-state',
		'aria-checked', 'id', 'for', 'target', 'aria-haspopup', 'aria-controls',
		'aria-owns', 'contenteditable',
	]

	function capText(text, maxLength) {
		return text.length > maxLength ? text.substring(0, maxLength) + '...' : text
	}

	function matchAttributes(attrs, patterns) {
		const result = {}
		for (const key of Object.keys(attrs)) {
			const value = attrs[key]
			if (patterns.includes(key) && value && value.trim()) result[key] = value.trim()
		}
		return result
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
		return parts.join('\n').trim()
	}

	function flatTreeToString(flatTree) {
		const root = buildTreeNode(flatTree, flatTree.rootId)
		if (!root) return ''
		setParents(root, null)

		const lines = []
		const selectorMap = {}

		function process(node, depth) {
			let nextDepth = depth
			const indent = '\t'.repeat(depth)

			if (node.type === 'element') {
				if (node.highlightIndex !== undefined) {
					nextDepth += 1
					selectorMap[node.highlightIndex] = node.ref

					const text = textOf(node)
					const attrsToInclude = matchAttributes(node.attributes, DEFAULT_INCLUDE_ATTRIBUTES)
					if (attrsToInclude.role === node.tagName) delete attrsToInclude.role
					for (const attr of ['aria-label', 'placeholder', 'title']) {
						if (attrsToInclude[attr] && attrsToInclude[attr].toLowerCase().trim() === text.toLowerCase().trim()) {
							delete attrsToInclude[attr]
						}
					}
					const attrStr = Object.entries(attrsToInclude)
						.map(([k, v]) => `${k}=${capText(v, 20)}`)
						.join(' ')

					let line = `${indent}[${node.highlightIndex}]<${node.tagName}`
					if (attrStr) line += ` ${attrStr}`
					if (text) {
						line += (attrStr ? '' : ' ') + `>${text.trim()}`
					} else if (!attrStr) {
						line += ' '
					}
					line += ' />'
					lines.push(line)
				}
				for (const child of node.children) process(child, nextDepth)
			} else if (node.type === 'text') {
				if (hasParentWithHighlight(node)) return
				if (node.parent && node.parent.type === 'element' && node.parent.isVisible && node.parent.isTopElement) {
					lines.push(`${indent}${node.text || ''}`)
				}
			}
		}

		process(root, 0)
		return { text: lines.join('\n'), selectorMap }
	}

	function getTree(viewportOnly) {
		const flatTree = window.__bmcpBuildDomTree({
			doHighlightElements: false,
			focusHighlightIndex: -1,
			viewportExpansion: viewportOnly ? 0 : -1,
			debugMode: false,
			interactiveBlacklist: [],
			interactiveWhitelist: [],
			highlightOpacity: 0,
			highlightLabelOpacity: 0,
		})
		const { text, selectorMap } = flatTreeToString(flatTree)
		window.__bmcpSelectorMap = selectorMap
		return {
			url: window.location.href,
			title: document.title,
			elementCount: Object.keys(selectorMap).length,
			tree: text,
		}
	}

	function elementByIndex(index) {
		const map = window.__bmcpSelectorMap || {}
		const el = map[index]
		if (!el) throw new Error(`No element at index ${index}. Call getTree() first (or the DOM changed).`)
		return el
	}

	function getRect(index) {
		const el = elementByIndex(index)
		el.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' })
		const rect = el.getBoundingClientRect()
		return {
			x: rect.left + rect.width / 2,
			y: rect.top + rect.height / 2,
			width: rect.width,
			height: rect.height,
			tagName: el.tagName.toLowerCase(),
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

	window.__bmcp = { getTree, getRect, getInputInfo, selectOption }
})();
