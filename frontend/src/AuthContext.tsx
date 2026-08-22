import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { api } from './api'

export type AuthUser = {
  id: string
  username: string
  role: 'user' | 'admin'
  status: 'active' | 'disabled'
  created_at: string
  updated_at: string
  last_login_at?: string | null
}

type AuthContextValue = {
  user: AuthUser | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  registerWithInvite: (username: string, password: string, invitationCode: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)

  async function refresh() {
    try {
      const response = await api<{ user: AuthUser }>('/auth/me')
      setUser(response.user)
    } catch {
      setUser(null)
    }
  }

  useEffect(() => {
    void refresh().finally(() => setLoading(false))
    const unauthorized = () => setUser(null)
    window.addEventListener('auth:unauthorized', unauthorized)
    return () => window.removeEventListener('auth:unauthorized', unauthorized)
  }, [])

  const value = useMemo<AuthContextValue>(() => ({
    user,
    loading,
    async login(username, password) {
      const response = await api<{ user: AuthUser }>('/auth/login', {
        method: 'POST',
        body: { username, password },
      })
      setUser(response.user)
    },
    async registerWithInvite(username, password, invitationCode) {
      const response = await api<{ user: AuthUser }>('/auth/register-with-invite', {
        method: 'POST',
        body: { username, password, invitation_code: invitationCode },
      })
      setUser(response.user)
    },
    async logout() {
      await api('/auth/logout', { method: 'POST' })
      setUser(null)
    },
    refresh,
  }), [user, loading])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('AuthProvider is missing.')
  return value
}
