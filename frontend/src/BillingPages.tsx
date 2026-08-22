import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, CheckCircle2, Clapperboard, Clock3, CreditCard, ReceiptText, XCircle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, assetUrl } from './api'
import { AccountControls } from './AuthPages'

type Wallet = {
  available_balance: number
  frozen_balance: number
  lifetime_granted: number
  lifetime_recharged: number
  lifetime_consumed: number
  lifetime_refunded: number
}
type LedgerItem = {
  id: string; type: string; amount: number; available_before: number; available_after: number
  frozen_before: number; frozen_after: number; created_at: string
}
type Package = { package_code: string; credit_amount: number; amount_cny_fen: number }
type BillingConfig = {
  recharge_instructions: string; packages: Package[]; wechat_qr_available: boolean; wechat_qr_url?: string | null
}
type RechargeRequest = {
  id: string; package_code: string; amount_cny_fen: number; credit_amount: number; payment_time: string
  payment_note?: string | null; status: 'submitted' | 'credited' | 'rejected'; review_note?: string | null
  created_at: string; screenshot_url?: string | null
}

const typeLabels: Record<string, string> = {
  grant: '赠送', recharge: '充值', freeze: '冻结', consume: '消费', release: '释放', refund: '退款', manual_adjustment: '人工调整',
}

function BillingShell({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="billing-page"><header className="topbar"><Link to="/" className="brand"><span className="brand-mark"><Clapperboard size={17} /></span><span>Drama AI Tool</span></Link><AccountControls /></header><main className="billing-content"><div className="billing-heading"><Link className="text-button" to="/"><ArrowLeft size={15} /> 返回项目</Link><h1>{title}</h1></div>{children}</main></div>
}

function WalletSummary({ wallet }: { wallet: Wallet }) {
  return <section className="wallet-summary">
    <article><span>可用积分</span><strong>{wallet.available_balance}</strong></article>
    <article><span>冻结积分</span><strong>{wallet.frozen_balance}</strong></article>
    <article><span>累计赠送</span><strong>{wallet.lifetime_granted}</strong></article>
    <article><span>累计充值</span><strong>{wallet.lifetime_recharged}</strong></article>
    <article><span>累计消费</span><strong>{wallet.lifetime_consumed}</strong></article>
    <article><span>累计退款</span><strong>{wallet.lifetime_refunded}</strong></article>
  </section>
}

export function CreditsPage() {
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [ledger, setLedger] = useState<LedgerItem[]>([])
  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    Promise.all([
      api<Wallet>('/credits/me'),
      api<{ items: LedgerItem[] }>(`/credits/ledger${filter ? `?type=${encodeURIComponent(filter)}` : ''}`),
    ]).then(([balance, entries]) => { setWallet(balance); setLedger(entries.items) }).catch((reason) => setError(String(reason)))
  }, [filter])
  return <BillingShell title="积分明细">
    {error && <div className="auth-error">{error.split('\n')[0]}</div>}
    {wallet && <WalletSummary wallet={wallet} />}
    <div className="billing-toolbar"><label>流水类型<select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="">全部</option>{Object.entries(typeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><Link className="primary-button" to="/recharge"><CreditCard size={16} /> 充值积分</Link></div>
    <section className="billing-table"><div className="billing-table-head"><span>类型</span><span>积分</span><span>可用余额</span><span>冻结余额</span><span>时间</span></div>{ledger.map((item) => <div className="billing-table-row" key={item.id}><strong>{typeLabels[item.type] || item.type}</strong><span>{item.amount}</span><span>{item.available_before} → {item.available_after}</span><span>{item.frozen_before} → {item.frozen_after}</span><time>{new Date(item.created_at).toLocaleString()}</time></div>)}{!ledger.length && <p className="billing-empty">暂无积分流水</p>}</section>
  </BillingShell>
}

export function RechargePage() {
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [config, setConfig] = useState<BillingConfig | null>(null)
  const [requests, setRequests] = useState<RechargeRequest[]>([])
  const [packageCode, setPackageCode] = useState('package_360')
  const [paymentTime, setPaymentTime] = useState(() => new Date().toISOString().slice(0, 16))
  const [paymentNote, setPaymentNote] = useState('')
  const [screenshot, setScreenshot] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const selected = useMemo(() => config?.packages.find((item) => item.package_code === packageCode), [config, packageCode])
  async function load() {
    const [balance, settings, history] = await Promise.all([
      api<Wallet>('/credits/me'), api<BillingConfig>('/billing/config'), api<{ items: RechargeRequest[] }>('/recharge-requests'),
    ])
    setWallet(balance); setConfig(settings); setRequests(history.items)
    if (!settings.packages.some((item) => item.package_code === packageCode)) setPackageCode(settings.packages[0]?.package_code || '')
  }
  useEffect(() => { void load().catch((reason) => setError(String(reason))) }, [])
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!paymentNote.trim()) {
      setError('请填写付款人微信号或联系邮箱。')
      return
    }
    setBusy(true); setError('')
    const form = new FormData()
    form.append('package_code', packageCode)
    form.append('payment_time', new Date(paymentTime).toISOString())
    form.append('payment_note', paymentNote.trim())
    if (screenshot) form.append('screenshot', screenshot)
    try {
      await api('/recharge-requests', { method: 'POST', body: form })
      setPaymentNote(''); setScreenshot(null)
      await load()
    } catch (reason) { setError(String(reason).split('\n')[0]) } finally { setBusy(false) }
  }
  return <BillingShell title="充值积分">
    {error && <div className="auth-error">{error}</div>}
    {wallet && <WalletSummary wallet={wallet} />}
    <section className="recharge-layout"><div className="billing-section"><div className="section-heading-inline"><div><span className="section-label">WECHAT</span><h2>微信付款</h2></div><CreditCard size={20} /></div>{config?.wechat_qr_url ? <img className="wechat-qr" src={assetUrl(config.wechat_qr_url)} alt="微信收款码" /> : <div className="qr-placeholder">管理员尚未配置收款码</div>}<p className="billing-note">{config?.recharge_instructions}</p></div><form className="billing-section recharge-form" onSubmit={submit}><div className="section-heading-inline"><div><span className="section-label">PACKAGE</span><h2>提交充值申请</h2></div><ReceiptText size={20} /></div><div className="package-grid">{config?.packages.map((item) => <button type="button" key={item.package_code} className={packageCode === item.package_code ? 'selected' : ''} onClick={() => setPackageCode(item.package_code)}><strong>{item.credit_amount}积分</strong><span>¥{(item.amount_cny_fen / 100).toFixed(0)}</span></button>)}</div><div className="selected-package">本次申请：<strong>{selected?.credit_amount || 0}积分</strong> · ¥{((selected?.amount_cny_fen || 0) / 100).toFixed(0)}</div><label>付款时间<input type="datetime-local" required value={paymentTime} onChange={(event) => setPaymentTime(event.target.value)} /></label><label>付款人微信号 / 联系邮箱（必填）<textarea required maxLength={500} value={paymentNote} onChange={(event) => setPaymentNote(event.target.value)} placeholder="填写微信号或联系邮箱，便于核对付款" /></label><label>付款截图（可选，最大5MB）<input type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => setScreenshot(event.target.files?.[0] || null)} /></label><button className="primary-button" disabled={busy || !selected || !paymentNote.trim()}>{busy ? '正在提交...' : '提交审核'}</button></form></section>
    <section className="billing-section"><div className="section-heading-inline"><div><span className="section-label">HISTORY</span><h2>充值申请</h2></div><Clock3 size={20} /></div><div className="recharge-history">{requests.map((item) => <article key={item.id}><div>{item.status === 'credited' ? <CheckCircle2 size={18} /> : item.status === 'rejected' ? <XCircle size={18} /> : <Clock3 size={18} />}<div><strong>{item.credit_amount}积分 · ¥{(item.amount_cny_fen / 100).toFixed(0)}</strong><span>{new Date(item.created_at).toLocaleString()}</span></div></div><div><span className={`status-chip status-${item.status}`}>{item.status === 'submitted' ? '待审核' : item.status === 'credited' ? '已到账' : '未通过'}</span>{item.review_note && <small>{item.review_note}</small>}</div></article>)}</div></section>
  </BillingShell>
}
