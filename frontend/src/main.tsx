import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './styles.css'
import { LocaleProvider } from './i18n'
import { AuthProvider } from './AuthContext'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <LocaleProvider>
        <AuthProvider><App /></AuthProvider>
      </LocaleProvider>
    </BrowserRouter>
  </React.StrictMode>,
)
