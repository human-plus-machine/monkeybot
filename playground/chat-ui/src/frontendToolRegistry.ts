export type FrontendToolResult = {
  result: Array<Record<string, unknown>>
  is_error: boolean
}

export type FrontendTool = (args: Record<string, unknown>) => Promise<FrontendToolResult>

/** Mutable registry — tests may assign handlers. */
export const frontendToolRegistry: Record<string, FrontendTool> = {}

export function register(name: string, handler: FrontendTool): void {
  frontendToolRegistry[name] = handler
}

export function unregister(name: string): void {
  delete frontendToolRegistry[name]
}

export async function invokeFrontendTool(name: string, args: Record<string, unknown>): Promise<FrontendToolResult> {
  const fn = frontendToolRegistry[name]
  if (!fn) {
    throw new Error(`no frontend handler for tool '${name}'`)
  }
  return fn(args)
}
