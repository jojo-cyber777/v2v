import { useEffect, useState } from 'react'
import { Coins } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { api } from './api'

type Balance = { available_balance: number; frozen_balance: number }

export function CreditBalanceBadge() {
  const navigate = useNavigate()
  const [balance, setBalance] = useState<Balance | null>(null)
  useEffect(() => {
    const load = () => void api<Balance>('/credits/me').then(setBalance).catch(() => setBalance(null))
    load()
    window.addEventListener('credits:updated', load)
    window.addEventListener('focus', load)
    return () => {
      window.removeEventListener('credits:updated', load)
      window.removeEventListener('focus', load)
    }
  }, [])
  if (!balance) return null
  return <button className="credit-balance-badge" title="积分明细" onClick={() => navigate('/credits')}>
    <Coins size={15} /><span>积分：{balance.available_balance}</span>
  </button>
}
