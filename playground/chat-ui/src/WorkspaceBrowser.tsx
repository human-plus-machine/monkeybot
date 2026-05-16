import { useCallback, useEffect, useState } from 'react'
import { fetchWorkspaceFile, fetchWorkspaceTree, type WorkspaceTreeEntry } from './gatewayClient'

function IconRefresh() {
  return (
    <svg
      className="btn-icon-svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.85"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 3" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 21" />
      <path d="M3 21v-5h5" />
    </svg>
  )
}

function IconChevronLeft() {
  return (
    <svg
      className="btn-icon-svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="m15 18-6-6 6-6" />
    </svg>
  )
}

export default function WorkspaceBrowser() {
  /** Stack of repo-relative directory paths; `['']` = workspace root. */
  const [dirStack, setDirStack] = useState<string[]>([''])
  const dirPath = dirStack[dirStack.length - 1] ?? ''
  const [entries, setEntries] = useState<WorkspaceTreeEntry[]>([])
  const [treePathLabel, setTreePathLabel] = useState('.')
  const [loadingTree, setLoadingTree] = useState(false)
  const [treeError, setTreeError] = useState<string | null>(null)

  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [fileBody, setFileBody] = useState<string | null>(null)
  const [fileMeta, setFileMeta] = useState('')
  const [loadingFile, setLoadingFile] = useState(false)
  const [fileError, setFileError] = useState<string | null>(null)

  const loadTree = useCallback(async () => {
    setLoadingTree(true)
    setTreeError(null)
    try {
      const data = await fetchWorkspaceTree(dirPath || undefined)
      setEntries(data.entries)
      setTreePathLabel(data.path)
    } catch (e) {
      setTreeError(e instanceof Error ? e.message : String(e))
      setEntries([])
    } finally {
      setLoadingTree(false)
    }
  }, [dirPath])

  useEffect(() => {
    void loadTree()
  }, [loadTree])

  const clearFilePreview = useCallback(() => {
    setSelectedFile(null)
    setFileBody(null)
    setFileMeta('')
    setFileError(null)
  }, [])

  const goBack = useCallback(() => {
    setDirStack((s) => (s.length <= 1 ? s : s.slice(0, -1)))
    clearFilePreview()
  }, [clearFilePreview])

  const openDir = (p: string) => {
    clearFilePreview()
    setDirStack((s) => [...s, p])
  }

  const openFile = async (p: string) => {
    setSelectedFile(p)
    setLoadingFile(true)
    setFileError(null)
    setFileBody(null)
    setFileMeta('')
    try {
      const f = await fetchWorkspaceFile(p)
      setFileBody(f.content)
      setFileMeta(
        `Lines ${f.start_line}-${f.end_line} of ${f.total_lines}${f.truncated ? ' (truncated)' : ''}`,
      )
    } catch (e) {
      setFileError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingFile(false)
    }
  }

  const atRoot = dirStack.length <= 1

  return (
    <div className="workspace-browser">
      <p className="workspace-browser-intro muted">
        Read-only view of the gateway process working directory (same bind mount as the agent).
      </p>
      <div className="workspace-toolbar">
        <button
          type="button"
          className="btn btn-icon workspace-toolbar-back"
          onClick={goBack}
          disabled={atRoot}
          aria-label="Back to parent folder"
          title="Back to parent folder"
        >
          <IconChevronLeft />
        </button>
        <code className="workspace-cwd" title={dirPath || '.'}>
          {treePathLabel}
        </code>
        <div className="workspace-toolbar-actions">
          <button
            type="button"
            className="btn btn-icon"
            aria-label="Refresh listing"
            title="Refresh listing"
            onClick={() => void loadTree()}
            disabled={loadingTree}
          >
            <IconRefresh />
          </button>
        </div>
      </div>
      {treeError ? <p className="workspace-error">{treeError}</p> : null}
      <div className="workspace-split">
        <div className="workspace-list-pane">
          <div className="workspace-list-scroll" aria-busy={loadingTree} aria-label="Directory listing">
            <div role="list">
              {loadingTree && entries.length === 0 ? (
                <p className="workspace-placeholder muted">Loading…</p>
              ) : null}
              {!loadingTree && entries.length === 0 && !treeError ? (
                <p className="workspace-placeholder muted">Empty directory.</p>
              ) : null}
              {entries.map((e) => (
                <div key={e.path} role="listitem">
                  {e.kind === 'dir' ? (
                    <button
                      type="button"
                      className="workspace-row workspace-row--dir"
                      onClick={() => openDir(e.path)}
                    >
                      <span className="workspace-kind" aria-hidden>
                        /
                      </span>
                      <span className="workspace-name">{e.name}</span>
                    </button>
                  ) : (
                    <button
                      type="button"
                      className={`workspace-row workspace-row--file${selectedFile === e.path ? ' workspace-row--active' : ''}`}
                      onClick={() => void openFile(e.path)}
                    >
                      <span className="workspace-kind" aria-hidden>
                        ·
                      </span>
                      <span className="workspace-name">{e.name}</span>
                    </button>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
        <div className="workspace-file-pane">
          <div className="workspace-file-header">
            {selectedFile ? (
              <code className="workspace-file-path" title={selectedFile}>
                {selectedFile}
              </code>
            ) : (
              <span className="muted">Select a file to preview</span>
            )}
          </div>
          {fileError ? <p className="workspace-error workspace-error--tight">{fileError}</p> : null}
          {fileMeta ? <p className="workspace-file-meta muted">{fileMeta}</p> : null}
          <div className="workspace-file-scroll" tabIndex={0} aria-label="File preview">
            {loadingFile ? <p className="workspace-placeholder muted">Loading file…</p> : null}
            {!loadingFile && fileBody != null ? <pre className="workspace-file-pre">{fileBody}</pre> : null}
            {!loadingFile && selectedFile && fileBody == null && !fileError ? (
              <p className="workspace-placeholder muted">No content loaded.</p>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  )
}
