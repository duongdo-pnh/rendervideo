import { createSlice } from '@reduxjs/toolkit'

export type Theme = 'dark' | 'light'

function readInitialTheme(): Theme {
  try {
    const stored = localStorage.getItem('theme')
    if (stored === 'dark' || stored === 'light') return stored
  } catch {
    // localStorage unavailable
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle('dark', theme === 'dark')
  try {
    localStorage.setItem('theme', theme)
  } catch {
    // ignore
  }
}

interface UiState {
  theme: Theme
}

const initialState: UiState = { theme: readInitialTheme() }
applyTheme(initialState.theme)

const uiSlice = createSlice({
  name: 'ui',
  initialState,
  reducers: {
    toggleTheme(state) {
      state.theme = state.theme === 'dark' ? 'light' : 'dark'
      applyTheme(state.theme)
    },
  },
})

export const { toggleTheme } = uiSlice.actions
export default uiSlice.reducer
