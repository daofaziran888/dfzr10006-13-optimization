import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/orchestra'
const columns = [
  "流程编号",
  "流程名称",
  "编排步骤",
  "并行策略",
  "超时控制",
  "失败回滚",
  "执行日志",
  "流程状态"
]
const actions = [
  "启动编排",
  "预演编排",
  "执行编排",
  "回滚流程",
  "归档流程"
]
const statuses = [
  "草稿",
  "运行中",
  "已完成",
  "已回滚"
]
const stats = [
  {
    "label": "编排流程总数",
    "value": 0
  },
  {
    "label": "运行中流程",
    "value": 0
  },
  {
    "label": "已完成流程",
    "value": 0
  }
]

type Row = Record<string, any>
type Report = Record<string, any>
type RuleEntry = Record<string, any>

const defaultTimeout = 90

export default function OrchestraView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [timeoutSeconds, setTimeoutSeconds] = useState(defaultTimeout)
  const [ruleVersion, setRuleVersion] = useState('')
  const [ruleEntries, setRuleEntries] = useState<RuleEntry[]>([])
  const [report, setReport] = useState<Report | null>(null)
  const [reportTitle, setReportTitle] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [listResponse, ruleResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/rules`),
      ])
      if (!listResponse.ok) {
        throw new Error('编排流程列表读取失败')
      }
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (ruleResponse.ok) {
        const rulePayload = await ruleResponse.json()
        setRuleVersion(rulePayload.rule_version ?? '')
        setRuleEntries(rulePayload.entries ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务编排组合列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, timeout_seconds: timeoutSeconds } }),
      })
      if (!response.ok) {
        throw new Error('服务编排组合动作未生效，请稍后重试')
      }
      const payload = await response.json()
      setReport(payload.result ?? null)
      setReportTitle(`${row['流程编号'] ?? row.id} · ${action}`)
      if (!payload.ok) {
        setErrorMessage(payload.message)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务编排组合操作失败')
    }
  }

  return (
    <section className="page" data-module="orchestra">
      <header className="page-head">
        <div>
          <h2>服务编排组合管理</h2>
          <p className="page-desc">
            预演、真实执行与回滚引用同一规则版本 {ruleVersion || '—'}；并行分组在判定前一次性编译，
            资源争用先按优先级裁决，再输出可执行步骤与阻塞步骤。
          </p>
        </div>
        <div className="page-actions">
          <label className="filter-item">
            <span>超时上限（秒）</span>
            <input
              type="number"
              min={1}
              value={timeoutSeconds}
              onChange={(event) => setTimeoutSeconds(Number(event.target.value) || defaultTimeout)}
            />
          </label>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出服务编排组合清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item, index) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">
              {index === 0 ? total : index === 1
                ? rows.filter((row) => row['流程状态'] === '运行中').length
                : rows.filter((row) => row['流程状态'] === '已完成').length}
            </strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无服务编排组合数据，可先登记编排流程</td>
            </tr>
          )}
        </tbody>
      </table>

      {report && (
        <section className="report-panel">
          <h3>预演 / 执行判定：{reportTitle}</h3>
          <p className="page-desc">
            {report.message ?? '—'}（规则版本 {report.rule_version ?? '—'}，超时 {report.timeout_seconds ?? '—'} 秒）
          </p>
          <div className="report-grid">
            <article className="stat-card">
              <span className="stat-label">可执行步骤</span>
              <strong className="stat-value">
                {(report.executable_steps ?? []).map((step: Row) => step.id).join('、') || '无'}
              </strong>
            </article>
            <article className="stat-card">
              <span className="stat-label">阻塞步骤</span>
              <strong className="stat-value">
                {(report.blocked_steps ?? []).map((step: Row) => `${step.id}(${step.rule_id})`).join('、') || '无'}
              </strong>
            </article>
            <article className="stat-card">
              <span className="stat-label">超时步骤</span>
              <strong className="stat-value">
                {(report.timed_out_steps ?? []).map((step: Row) => step.id).join('、') || '无'}
              </strong>
            </article>
            <article className="stat-card">
              <span className="stat-label">命中规则</span>
              <strong className="stat-value">{(report.rule_hits ?? []).join('、') || '无'}</strong>
            </article>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>步骤</th><th>分组</th><th>预估耗时</th><th>优先级</th><th>状态</th><th>规则</th><th>说明</th>
              </tr>
            </thead>
            <tbody>
              {(report.checklist ?? []).map((step: Row) => (
                <tr key={String(step.id)}>
                  <td>{step.id}</td>
                  <td>{step.group || '—'}</td>
                  <td>{step.estimated_seconds ?? '—'}</td>
                  <td>{step.priority ?? '—'}</td>
                  <td>{step.status ?? '—'}</td>
                  <td>{step.rule_id || '—'}</td>
                  <td>{step.reason || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {ruleEntries.length > 0 && (
        <section className="report-panel">
          <h3>共用规则表（规则版本 {ruleVersion}）</h3>
          <table className="data-table">
            <thead>
              <tr><th>规则编号</th><th>名称</th><th>适用阶段</th><th>条件</th><th>输出</th></tr>
            </thead>
            <tbody>
              {ruleEntries.map((entry) => (
                <tr key={entry.id}>
                  <td>{entry.id}</td>
                  <td>{entry.name}</td>
                  <td>{(entry.stages ?? []).join('、')}</td>
                  <td>{entry.condition}</td>
                  <td>{entry.outcome}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条服务编排组合记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
