import { useEffect, useRef, useState } from 'react'
import { CheckCircle2, Coins, CreditCard, ImageUp, RefreshCw, Search, XCircle } from 'lucide-react'
import { api, assetUrl } from './api'
import { XmaxTemporaryKeyAdminPanel } from './XmaxTemporaryKeyAdminPanel'

type AdminUser = { id: string; username: string; role: string; status: string }
type Wallet = { available_balance: number; frozen_balance: number; lifetime_granted: number; lifetime_recharged: number; lifetime_consumed: number; lifetime_refunded: number }
type Ledger = { id: string; type: string; amount: number; available_after: number; frozen_after: number; note?: string | null; created_at: string }
type Recharge = { id: string; username: string; user_id: string; credit_amount: number; amount_cny_fen: number; payment_time: string; payment_note?: string | null; status: string; screenshot_url?: string | null; review_note?: string | null }
type BillingConfig = { recharge_instructions: string; wechat_qr_url?: string | null }

const PAGE_SIZE = 20
const LEDGER_TYPES = [
  ['', '全部'],
  ['grant', 'grant'],
  ['recharge', 'recharge'],
  ['freeze', 'freeze'],
  ['consume', 'consume'],
  ['release', 'release'],
  ['refund', 'refund'],
  ['manual_adjustment', 'adjustment'],
] as const
const messageOf = (reason: unknown) => (reason instanceof Error ? reason.message : String(reason)).split('\n')[0]

export function AdminBillingPanel({ users }: { users: AdminUser[] }) {
  const [selectedUserId, setSelectedUserId] = useState('')
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [ledger, setLedger] = useState<Ledger[]>([])
  const [ledgerType, setLedgerType] = useState('')
  const [ledgerPage, setLedgerPage] = useState(1)
  const [ledgerTotal, setLedgerTotal] = useState(0)
  const [requests, setRequests] = useState<Recharge[]>([])
  const [requestTotal, setRequestTotal] = useState(0)
  const [requestPage, setRequestPage] = useState(1)
  const [requestUsername, setRequestUsername] = useState('')
  const [requestStatus, setRequestStatus] = useState('')
  const [requestFrom, setRequestFrom] = useState('')
  const [requestTo, setRequestTo] = useState('')
  const [config, setConfig] = useState<BillingConfig | null>(null)
  const [operation, setOperation] = useState<'add' | 'subtract'>('add')
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')
  const [adjustConfirm, setAdjustConfirm] = useState(false)
  const [reviewing, setReviewing] = useState<Recharge | null>(null)
  const [reviewAction, setReviewAction] = useState<'approve' | 'reject'>('approve')
  const [reviewNote, setReviewNote] = useState('')
  const [instructions, setInstructions] = useState('')
  const [qrFile, setQrFile] = useState<File | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const adjustmentKey = useRef(crypto.randomUUID())

  async function loadUser(userId: string, page = 1, append = false) {
    if (!userId) { setWallet(null); setLedger([]); setLedgerTotal(0); return }
    const query = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
    if (ledgerType) query.set('entry_type', ledgerType)
    const response = await api<{ wallet: Wallet; ledger: { items: Ledger[]; total: number } }>(`/admin/credits/users/${userId}?${query}`)
    setWallet(response.wallet)
    setLedger((current) => append ? [...current, ...response.ledger.items] : response.ledger.items)
    setLedgerTotal(response.ledger.total)
    setLedgerPage(page)
  }

  async function loadRequests(page = 1, append = false) {
    const query = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
    if (requestUsername.trim()) query.set('username', requestUsername.trim())
    if (requestStatus) query.set('status', requestStatus)
    if (requestFrom) query.set('created_from', `${requestFrom}T00:00:00`)
    if (requestTo) query.set('created_to', `${requestTo}T23:59:59`)
    const response = await api<{ items: Recharge[]; total: number }>(`/admin/recharge-requests?${query}`)
    setRequests((current) => append ? [...current, ...response.items] : response.items)
    setRequestTotal(response.total)
    setRequestPage(page)
  }

  async function loadSettings() {
    const settings = await api<BillingConfig>('/billing/config')
    setConfig(settings)
    setInstructions(settings.recharge_instructions)
  }

  useEffect(() => {
    if (!selectedUserId && users[0]) setSelectedUserId(users[0].id)
  }, [users, selectedUserId])
  useEffect(() => { void loadUser(selectedUserId).catch((cause) => setError(messageOf(cause))) }, [selectedUserId, ledgerType])
  useEffect(() => { void Promise.all([loadRequests(), loadSettings()]).catch((cause) => setError(messageOf(cause))) }, [])

  async function submitAdjustment() {
    if (!selectedUserId || !adjustConfirm) return
    setBusy('adjust'); setError('')
    try {
      await api(`/admin/credits/users/${selectedUserId}/adjust`, {
        method: 'POST',
        body: { operation, amount: Number(amount), reason, idempotency_key: adjustmentKey.current },
      })
      adjustmentKey.current = crypto.randomUUID()
      setAmount(''); setReason(''); setAdjustConfirm(false)
      await loadUser(selectedUserId)
    } catch (cause) { setError(messageOf(cause)) } finally { setBusy('') }
  }

  async function submitReview() {
    if (!reviewing) return
    setBusy(`review:${reviewing.id}`); setError('')
    try {
      await api(`/admin/recharge-requests/${reviewing.id}/${reviewAction}`, { method: 'POST', body: { review_note: reviewNote } })
      const creditedUserId = reviewing.user_id
      setReviewing(null); setReviewNote('')
      await loadRequests()
      if (creditedUserId === selectedUserId) await loadUser(selectedUserId)
    } catch (cause) { setError(messageOf(cause)) } finally { setBusy('') }
  }

  async function saveSettings(event: React.FormEvent) {
    event.preventDefault(); setBusy('settings'); setError('')
    const form = new FormData(); form.append('instructions', instructions)
    if (qrFile) form.append('wechat_qr', qrFile)
    try {
      await api('/admin/billing/settings', { method: 'PUT', body: form })
      setQrFile(null); await loadSettings()
    } catch (cause) { setError(messageOf(cause)) } finally { setBusy('') }
  }

  return <section className="admin-billing-panel">
    <div className="section-heading-inline"><div><span className="section-label">CREDITS & BILLING</span><h2>积分与充值</h2></div><Coins size={20} /></div>
    {error && <div className="auth-error">{error}</div>}
    <div className="admin-billing-grid">
      <div className="admin-section">
        <div className="section-heading-inline"><div><span className="section-label">WALLET</span><h3>用户积分</h3></div><RefreshCw size={18} /></div>
        <label>账号<select value={selectedUserId} onChange={(event) => setSelectedUserId(event.target.value)}>{users.map((item) => <option key={item.id} value={item.id}>{item.username} · {item.role} · {item.status}</option>)}</select></label>
        {wallet && <div className="compact-wallet"><span>可用<strong>{wallet.available_balance}</strong></span><span>冻结<strong>{wallet.frozen_balance}</strong></span><span>累计消费<strong>{wallet.lifetime_consumed}</strong></span></div>}
        <section className="admin-adjustment-section">
          <h4>人工积分调整</h4>
          <div className="adjustment-form"><label>操作<select value={operation} onChange={(event) => setOperation(event.target.value as 'add' | 'subtract')}><option value="add">增加</option><option value="subtract">扣减</option></select></label><label>积分<input type="number" min="1" step="1" value={amount} onChange={(event) => setAmount(event.target.value)} /></label><label className="wide-field">原因<input maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} /></label><label className="check-row wide-field"><input type="checkbox" checked={adjustConfirm} onChange={(event) => setAdjustConfirm(event.target.checked)} />确认对该用户执行本次人工调整</label><button className="primary-button wide-field" disabled={busy === 'adjust' || !selectedUserId || !Number.isInteger(Number(amount)) || Number(amount) <= 0 || !reason.trim() || !adjustConfirm} onClick={() => void submitAdjustment()}>{busy === 'adjust' ? '处理中...' : '提交调整'}</button></div>
        </section>
        <section className="admin-ledger-section">
          <h4>积分流水</h4>
          <div className="admin-query-toolbar"><label>流水类型<select value={ledgerType} onChange={(event) => setLedgerType(event.target.value)}>{LEDGER_TYPES.map(([value, label]) => <option key={value || 'all'} value={value}>{label}</option>)}</select></label></div>
          <div className="admin-ledger-list">{ledger.map((item) => <article key={item.id}><div><strong>{item.type} · {item.amount}</strong><span>{new Date(item.created_at).toLocaleString()}</span></div><span>可用 {item.available_after} · 冻结 {item.frozen_after}</span>{item.note && <small>{item.note}</small>}</article>)}</div>
          {ledger.length < ledgerTotal && <button className="text-button admin-load-more" onClick={() => void loadUser(selectedUserId, ledgerPage + 1, true)}>查看更多</button>}
        </section>
      </div>
      <form className="admin-section billing-settings-form" onSubmit={saveSettings}>
        <div className="section-heading-inline"><div><span className="section-label">PAYMENT</span><h3>收款设置</h3></div><ImageUp size={18} /></div>
        {config?.wechat_qr_url ? <img className="wechat-qr compact" src={assetUrl(config.wechat_qr_url)} alt="当前微信收款码" /> : <div className="qr-placeholder">尚未配置收款码</div>}
        <label>充值说明<textarea required maxLength={2000} value={instructions} onChange={(event) => setInstructions(event.target.value)} /></label>
        <label>替换微信收款码<input type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => setQrFile(event.target.files?.[0] || null)} /></label>
        <p className="billing-note">替换后旧图片保留，仅新图片成为当前收款码。</p>
        <button className="secondary-button" disabled={busy === 'settings' || !instructions.trim()}>{busy === 'settings' ? '保存中...' : '保存收款设置'}</button>
      </form>
    </div>
    <div className="admin-section recharge-review-section">
      <div className="section-heading-inline"><div><span className="section-label">REVIEW</span><h3>充值审核</h3></div><CreditCard size={18} /></div>
      <div className="admin-query-toolbar">
        <label>用户名<input value={requestUsername} onChange={(event) => setRequestUsername(event.target.value)} /></label>
        <label>状态<select value={requestStatus} onChange={(event) => setRequestStatus(event.target.value)}><option value="">全部</option><option value="submitted">待审核</option><option value="credited">已到账</option><option value="rejected">已拒绝</option></select></label>
        <label>付款日期从<input type="date" value={requestFrom} onChange={(event) => setRequestFrom(event.target.value)} /></label>
        <label>到<input type="date" value={requestTo} onChange={(event) => setRequestTo(event.target.value)} /></label>
        <button className="secondary-button" onClick={() => void loadRequests()}><Search size={15} />查询</button>
      </div>
      <div className="admin-recharge-list">{requests.map((item) => <article key={item.id}><div><strong>{item.username} · {item.credit_amount}积分 · ¥{(item.amount_cny_fen / 100).toFixed(0)}</strong><span>付款时间 {new Date(item.payment_time).toLocaleString()}</span><small>联系方式：{item.payment_note || '缺失'}</small></div><div>{item.screenshot_url && <a className="text-button" href={assetUrl(item.screenshot_url)} target="_blank" rel="noreferrer">查看截图</a>}<span className={`status-chip status-${item.status}`}>{item.status === 'submitted' ? '待审核' : item.status === 'credited' ? '已到账' : '已拒绝'}</span>{item.status === 'submitted' && <><button className="text-button" onClick={() => { setReviewing(item); setReviewAction('approve'); setReviewNote('') }}><CheckCircle2 size={15} />通过</button><button className="text-button danger" onClick={() => { setReviewing(item); setReviewAction('reject'); setReviewNote('') }}><XCircle size={15} />拒绝</button></>}</div></article>)}</div>
      {requests.length < requestTotal && <button className="text-button admin-load-more" onClick={() => void loadRequests(requestPage + 1, true)}>查看更多</button>}
      {reviewing && <div className="inline-confirmation"><strong>{reviewAction === 'approve' ? '确认充值到账' : '确认拒绝申请'}：{reviewing.username} · {reviewing.credit_amount}积分</strong><label>审核备注<textarea maxLength={500} value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} /></label><div><button className="secondary-button" onClick={() => setReviewing(null)}>取消</button><button className="primary-button" disabled={busy === `review:${reviewing.id}` || (reviewAction === 'reject' && !reviewNote.trim())} onClick={() => void submitReview()}>{busy === `review:${reviewing.id}` ? '处理中...' : '确认提交'}</button></div></div>}
    </div>
    <XmaxTemporaryKeyAdminPanel />
  </section>
}
