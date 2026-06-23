import '@testing-library/jest-dom'

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver = globalThis.ResizeObserver ?? ResizeObserverStub

if (typeof URL.createObjectURL !== 'function') {
  URL.createObjectURL = () => 'blob:mock-preview'
}
if (typeof URL.revokeObjectURL !== 'function') {
  URL.revokeObjectURL = () => {}
}
