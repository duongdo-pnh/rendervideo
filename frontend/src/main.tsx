import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import { store } from './store'
import { NotificationProvider } from './components/notifications/NotificationProvider'
import { ConfirmProvider } from './components/ConfirmDialog'
import { App } from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Provider store={store}>
      <NotificationProvider>
        <ConfirmProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ConfirmProvider>
      </NotificationProvider>
    </Provider>
  </StrictMode>,
)
