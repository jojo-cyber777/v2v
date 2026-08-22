import { useEffect, useState } from "react";
import {
  Check,
  Clapperboard,
  Copy,
  KeyRound,
  LogOut,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
} from "lucide-react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { api } from "./api";
import { type AuthUser, useAuth } from "./AuthContext";
import { AdminBillingPanel } from "./AdminBillingPanel";
import { CreditBalanceBadge } from "./CreditBalance";

function readableError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.split("\n")[0];
}

export function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (user) return <Navigate to="/" replace />;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(username, password);
      navigate("/", { replace: true });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthShell title="登录" copy="使用管理员创建的账号进入工作区。">
      <form className="auth-form" onSubmit={submit}>
        <label>
          账号
          <input
            autoFocus
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          密码
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && <div className="auth-error">{error}</div>}
        <button
          className="primary-button"
          disabled={busy || !username || !password}
        >
          {busy ? "正在登录..." : "登录"}
        </button>
      </form>
      <p className="auth-footnote">
        已有邀请码？ <Link to="/register">使用邀请码注册</Link>
      </p>
    </AuthShell>
  );
}

export function InviteRegistrationPage() {
  const { user, registerWithInvite } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [invitationCode, setInvitationCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (user) return <Navigate to="/" replace />;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await registerWithInvite(username, password, invitationCode);
      navigate("/", { replace: true });
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthShell
      title="邀请码注册"
      copy="Beta 不开放公共注册，每个邀请码默认只能使用一次。"
    >
      <form className="auth-form" onSubmit={submit}>
        <label>
          邀请码
          <input
            autoFocus
            value={invitationCode}
            onChange={(event) => setInvitationCode(event.target.value)}
          />
        </label>
        <label>
          账号
          <input
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          密码
          <input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && <div className="auth-error">{error}</div>}
        <button
          className="primary-button"
          disabled={busy || !username || !password || !invitationCode}
        >
          {busy ? "正在创建..." : "创建账号"}
        </button>
      </form>
      <p className="auth-footnote">
        已有账号？ <Link to="/login">返回登录</Link>
      </p>
    </AuthShell>
  );
}

function AuthShell({
  title,
  copy,
  children,
}: {
  title: string;
  copy: string;
  children: React.ReactNode;
}) {
  return (
    <main className="auth-page">
      <section className="auth-panel">
        <div className="auth-brand">
          <span className="brand-mark">
            <Clapperboard size={18} />
          </span>
          <strong>Drama AI Tool</strong>
        </div>
        <h1>{title}</h1>
        <p>{copy}</p>
        {children}
      </section>
    </main>
  );
}

export function AccountControls() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  if (!user) return null;
  return (
    <div className="account-controls">
      <CreditBalanceBadge />
      {user.role === "admin" && (
        <button
          className="icon-button"
          title="用户管理"
          onClick={() => navigate("/admin")}
        >
          <ShieldCheck size={17} />
        </button>
      )}
      <span className="account-name">{user.username}</span>
      <button
        className="icon-button"
        title="登出"
        onClick={() => void logout().then(() => navigate("/login"))}
      >
        <LogOut size={17} />
      </button>
    </div>
  );
}

type AdminUser = AuthUser & { project_count: number };
type Invitation = {
  id: string;
  expires_at: string;
  max_uses: number;
  used_count: number;
  status: string;
  created_by_username?: string;
  used_by_username?: string | null;
};

export function AdminPage() {
  const { user } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"user" | "admin">("user");
  const [confirmAdmin, setConfirmAdmin] = useState(false);
  const [inviteMaxUses, setInviteMaxUses] = useState(1);
  const [latestCode, setLatestCode] = useState("");
  const [error, setError] = useState("");
  if (!user || user.role !== "admin") return <Navigate to="/" replace />;

  async function load() {
    try {
      const [userResponse, invitationResponse] = await Promise.all([
        api<{ users: AdminUser[] }>("/admin/users"),
        api<{ invitations: Invitation[] }>("/admin/invitations"),
      ]);
      setUsers(userResponse.users);
      setInvitations(invitationResponse.invitations);
    } catch (reason) {
      setError(readableError(reason));
    }
  }
  useEffect(() => {
    void load();
  }, []);

  async function createUser(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api("/admin/users", {
        method: "POST",
        body: { username, password, role, confirm_admin: confirmAdmin },
      });
      setUsername("");
      setPassword("");
      setRole("user");
      setConfirmAdmin(false);
      await load();
    } catch (reason) {
      setError(readableError(reason));
    }
  }
  async function createInvitation() {
    setError("");
    try {
      const response = await api<{ invitation: Invitation & { code: string } }>(
        "/admin/invitations",
        {
          method: "POST",
          body: { expires_in_hours: 72, max_uses: inviteMaxUses },
        },
      );
      setLatestCode(response.invitation.code);
      await load();
    } catch (reason) {
      setError(readableError(reason));
    }
  }
  async function changeStatus(target: AdminUser) {
    setError("");
    try {
      await api(`/admin/users/${target.id}/status`, {
        method: "PATCH",
        body: { status: target.status === "active" ? "disabled" : "active" },
      });
      await load();
    } catch (reason) {
      setError(readableError(reason));
    }
  }
  async function deleteInvitation(invitation: Invitation) {
    setError("");
    try {
      await api(`/admin/invitations/${invitation.id}`, { method: "DELETE" });
      await load();
    } catch (reason) {
      setError(readableError(reason));
    }
  }

  return (
    <div className="admin-page">
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand-mark">
            <Clapperboard size={17} />
          </span>
          <span>Drama AI Tool</span>
        </Link>
        <AccountControls />
      </header>
      <main className="admin-content">
        <div className="admin-heading">
          <div>
            <span className="section-label">INVITE-ONLY BETA</span>
            <h1>账号与邀请</h1>
          </div>
          <div className="admin-invitation-create">
            <label>
              可使用次数
              <input
                type="number"
                min="1"
                max="1000"
                step="1"
                value={inviteMaxUses}
                onChange={(event) => setInviteMaxUses(Number(event.target.value))}
              />
            </label>
            <button
              className="secondary-button"
              disabled={!Number.isInteger(inviteMaxUses) || inviteMaxUses < 1}
              onClick={createInvitation}
            >
              <KeyRound size={16} /> 生成邀请码
            </button>
          </div>
        </div>
        {error && <div className="auth-error">{error}</div>}
        {latestCode && (
          <div className="invite-code">
            <div>
              <strong>邀请码只显示这一次</strong>
              <code>{latestCode}</code>
            </div>
            <button
              className="icon-button"
              title="复制邀请码"
              onClick={() => void navigator.clipboard.writeText(latestCode)}
            >
              <Copy size={16} />
            </button>
          </div>
        )}
        <section className="admin-grid">
          <div className="admin-section">
            <div className="section-heading-inline">
              <div>
                <span className="section-label">USERS</span>
                <h2>用户</h2>
              </div>
              <Users size={20} />
            </div>
            <div className="admin-list">
              {users.map((item) => (
                <article key={item.id}>
                  <div>
                    <strong>{item.username}</strong>
                    <span>
                      {item.role} · {item.project_count} 个项目
                    </span>
                  </div>
                  <div>
                    <span className={`status-chip status-${item.status}`}>
                      {item.status}
                    </span>
                    <button
                      className="text-button"
                      onClick={() => void changeStatus(item)}
                    >
                      {item.status === "active" ? "禁用" : "启用"}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </div>
          <div className="admin-section">
            <div className="section-heading-inline">
              <div>
                <span className="section-label">CREATE USER</span>
                <h2>直接创建账号</h2>
              </div>
              <UserPlus size={20} />
            </div>
            <form className="auth-form" onSubmit={createUser}>
              <label>
                账号
                <input
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                />
              </label>
              <label>
                初始密码
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </label>
              <label>
                角色
                <select
                  value={role}
                  onChange={(event) =>
                    setRole(event.target.value as "user" | "admin")
                  }
                >
                  <option value="user">普通用户</option>
                  <option value="admin">管理员</option>
                </select>
              </label>
              {role === "admin" && (
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={confirmAdmin}
                    onChange={(event) => setConfirmAdmin(event.target.checked)}
                  />
                  确认创建新的管理员
                </label>
              )}
              <button
                className="primary-button"
                disabled={
                  !username || !password || (role === "admin" && !confirmAdmin)
                }
              >
                创建账号
              </button>
            </form>
          </div>
        </section>
        <section className="admin-section">
          <div className="section-heading-inline">
            <div>
              <span className="section-label">INVITATIONS</span>
              <h2>邀请码</h2>
            </div>
            <KeyRound size={20} />
          </div>
          <div className="admin-list">
            {invitations.map((invitation) => (
              <article key={invitation.id}>
                <div>
                  <strong>{invitation.id}</strong>
                  <span>
                    到期 {new Date(invitation.expires_at).toLocaleString()} ·{" "}
                    {invitation.used_count}/{invitation.max_uses}
                  </span>
                </div>
                <div>
                  <span className={`status-chip status-${invitation.status}`}>
                    {invitation.status}
                  </span>
                  {invitation.status === "used" && <Check size={16} />}
                  {invitation.used_count === 0 && (
                    <button
                      className="text-button danger"
                      onClick={() => void deleteInvitation(invitation)}
                    >
                      <Trash2 size={14} /> 删除
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
        <AdminBillingPanel users={users} />
      </main>
    </div>
  );
}
