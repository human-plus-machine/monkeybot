# Third-party notice

`dom_tree.js` in this directory is vendored from [alibaba/page-agent](https://github.com/alibaba/page-agent) (`packages/page-controller/src/dom/dom_tree/index.js`), which itself is ported from [browser-use/browser-use](https://github.com/browser-use/browser-use). Both are MIT licensed.

Modification: the ESM `export default (...) => {...}` was converted to a plain `window.__bmcpBuildDomTree = function (...) {...}` assignment so the script can run as a raw CDP-injected snippet (no bundler/module loader on the target page). No other logic was changed.

`pa_driver.js` is original glue code written for browser-mcp, following the same tree-flattening approach as page-agent's `packages/page-controller/src/dom/index.ts` and `actions.ts` (also MIT licensed) but reimplemented to act via CDP-driven coordinates from the Python side rather than in-page synthetic event dispatch.

```
MIT License

Copyright (c) 2026 SimonLuvRamen
Copyright (c) 2026 Alibaba Group Holding Limited

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
