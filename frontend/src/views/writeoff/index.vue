<template>
  <section class="page" data-module="writeoff">
    <header class="page-head">
      <div>
        <h2>批量入账冲销</h2>
        <p class="page-desc">
          分析管道子模块：一次提交多条冲销行（借方行、贷方行、源回执、冲销原因），按活动与结算日逐行校验；
          同一借方行争用时按源回执时间先后确认，可部分确认，未覆盖部分记待补差额。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">提交冲销批次</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <h3 class="section-title">冲销批次</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in batchColumns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in batches" :key="String(row.批次号)">
          <td v-for="column in batchColumns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="selectBatch(row)">查看行回执</button>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td :colspan="batchColumns.length + 1" class="empty-state">暂无冲销批次，可先提交批次</td>
        </tr>
      </tbody>
    </table>

    <template v-if="batchDetail">
      <h3 class="section-title">批次 {{ batchDetail.批次号 }} 行回执（{{ batchDetail.status }}）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in lineColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="line in batchDetail.行回执" :key="String(line.id)">
            <td v-for="column in lineColumns" :key="column">{{ line[column] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </template>

    <h3 class="section-title">失败项复核（已拒绝 / 待复核，可多选重新开始）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>选择</th>
          <th v-for="column in failureColumns" :key="column">{{ column }}</th>
          <th>纠正结算日</th>
          <th>纠正金额</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in failures" :key="`${item.批次号}-${item.行号}`">
          <td>
            <input
              type="checkbox"
              :disabled="!item.可纠正"
              :checked="selectedFailures.has(item.批次号 + '|' + item.行号)"
              @change="toggleFailure(item)"
            />
          </td>
          <td v-for="column in failureColumns" :key="column">
            {{ column === '可纠正' ? (item.可纠正 ? '是' : '否') : (item[column] ?? '—') }}
          </td>
          <td><input v-model="corrections[item.行号 + '@结算日']" placeholder="YYYY-MM-DD" :disabled="!item.可纠正" /></td>
          <td><input v-model="corrections[item.行号 + '@金额']" placeholder="新金额" :disabled="!item.可纠正" /></td>
        </tr>
        <tr v-if="!failures.length">
          <td :colspan="failureColumns.length + 3" class="empty-state">暂无失败项</td>
        </tr>
      </tbody>
    </table>
    <div class="page-actions restart-bar">
      <button class="btn primary" type="button" :disabled="!selectedFailures.size" @click="restartSelected">
        重新开始所选可纠正条目（{{ selectedFailures.size }}）
      </button>
    </div>

    <h3 class="section-title">数据集市原流水</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in martColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in martFlows" :key="String(row.原流水号)">
          <td v-for="column in martColumns" :key="column">{{ row[column] ?? '—' }}</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">借方台账</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in ledgerColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in ledger" :key="String(row.借方行号)">
          <td v-for="column in ledgerColumns" :key="column">{{ row[column] ?? '—' }}</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 个冲销批次</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
    </footer>

    <Teleport to="body">
      <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
        <div class="modal-card wide">
          <header class="modal-head">
            <h3>提交冲销批次</h3>
            <button class="btn ghost" type="button" @click="showCreateModal = false">&times;</button>
          </header>
          <form class="modal-body" @submit.prevent="submitCreate">
            <div class="batch-head-fields">
              <label class="modal-field">
                <span>批次号（幂等键，重复提交不新增抵扣）</span>
                <input v-model="createForm.批次号" placeholder="如 WO-0001" required />
              </label>
              <label class="modal-field">
                <span>活动编号</span>
                <input v-model="createForm.活动编号" placeholder="ACT-2026-11" required />
              </label>
              <label class="modal-field">
                <span>结算日</span>
                <input v-model="createForm.结算日" placeholder="2026-11-20" required />
              </label>
            </div>
            <div class="page-actions">
              <button class="btn" type="button" @click="addLine">添加冲销行</button>
              <button class="btn" type="button" @click="fillContentionDemo">填入争用示例</button>
              <button class="btn" type="button" @click="fillAnomalyDemo">填入异常示例</button>
            </div>
            <table class="data-table">
              <thead>
                <tr>
                  <th v-for="column in createLineColumns" :key="column">{{ column }}</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(line, index) in createForm.行" :key="index">
                  <td v-for="column in createLineColumns" :key="column">
                    <input
                      v-model="line[column]"
                      :placeholder="column"
                      :required="column !== '源回执时间' && column !== '冲销原因'"
                    />
                  </td>
                  <td>
                    <button class="link" type="button" @click="createForm.行.splice(index, 1)">删除</button>
                  </td>
                </tr>
              </tbody>
            </table>
            <footer class="modal-foot">
              <button class="btn" type="button" @click="showCreateModal = false">取消</button>
              <button class="btn primary" type="submit">提交批次</button>
            </footer>
          </form>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/pipeline/writeoff'
const batchColumns = ['批次号', '活动编号', '结算日', 'status', '行数', '可入账净额', '确认条数', '拒绝条数', '待复核条数', '重复提交次数', '提交时间']
const lineColumns = ['行号', '借方行号', '贷方行号', '金额', '确认金额', '待补差额', 'status', '拒绝原因', '源回执号', '冲销原因', '处理轮次']
const failureColumns = ['批次号', '行号', '借方行号', '贷方行号', '金额', 'status', '拒绝原因', '可纠正']
const martColumns = ['原流水号', '借方行号', '活动编号', '结算日', '原始金额', '已确认净额', '待补差额', '状态']
const ledgerColumns = ['借方行号', '活动编号', '结算日', '原量', '已确认冲销', '余量']
const createLineColumns = ['借方行号', '贷方行号', '金额', '源回执号', '源回执时间', '冲销原因', '结算日']

const stats = ref([
  { label: '冲销批次', value: 0 },
  { label: '已入账净额合计', value: 0 },
  { label: '失败项（已拒绝/待复核）', value: 0 },
])

const batches = ref<Row[]>([])
const total = ref(0)
const batchDetail = ref<Row | null>(null)
const failures = ref<Row[]>([])
const martFlows = ref<Row[]>([])
const ledger = ref<Row[]>([])
const errorMessage = ref('')
const noticeMessage = ref('')
const showCreateModal = ref(false)
const createForm = ref<Row>({ 批次号: '', 活动编号: 'ACT-2026-11', 结算日: '2026-11-20', 行: [] })
const selectedFailures = ref<Set<string>>(new Set())
const corrections = ref<Record<string, string>>({})

function blankLine() {
  return { 借方行号: '', 贷方行号: '', 金额: '', 源回执号: '', 源回执时间: '', 冲销原因: '', 结算日: createForm.value.结算日 }
}

function addLine() {
  createForm.value.行.push(blankLine())
}

function openCreate() {
  errorMessage.value = ''
  createForm.value = { 批次号: `WO-${Date.now() % 100000}`, 活动编号: 'ACT-2026-11', 结算日: '2026-11-20', 行: [] }
  createForm.value.行 = [{ ...blankLine() }]
  showCreateModal.value = true
}

function fillContentionDemo() {
  createForm.value.活动编号 = 'ACT-2026-11'
  createForm.value.结算日 = '2026-11-20'
  createForm.value.行 = [
    { 借方行号: 'DBT-1001', 贷方行号: 'CR-01', 金额: '70', 源回执号: 'RCPT-001', 源回执时间: '2026-11-20 10:00:00', 冲销原因: '重复入账', 结算日: '2026-11-20' },
    { 借方行号: 'DBT-1001', 贷方行号: 'CR-02', 金额: '70', 源回执号: 'RCPT-002', 源回执时间: '2026-11-20 10:01:00', 冲销原因: '金额有误', 结算日: '2026-11-20' },
    { 借方行号: 'DBT-1002', 贷方行号: 'CR-03', 金额: '20', 源回执号: 'RCPT-003', 源回执时间: '2026-11-20 10:02:00', 冲销原因: '业务取消', 结算日: '2026-11-20' },
  ]
}

function fillAnomalyDemo() {
  createForm.value.活动编号 = 'ACT-2026-11'
  createForm.value.结算日 = '2026-11-20'
  createForm.value.行 = [
    { 借方行号: 'DBT-1003', 贷方行号: 'CR-X1', 金额: '20', 源回执号: 'RCPT-X1', 源回执时间: '2026-11-20 10:00:00', 冲销原因: '重复入账', 结算日: '2026-11-21' },
    { 借方行号: 'DBT-1003', 贷方行号: 'CR-X2', 金额: '10', 源回执号: 'RCPT-X2', 源回执时间: '2026-11-20 10:01:00', 冲销原因: '重复入账', 结算日: '2026-11-20' },
    { 借方行号: 'DBT-1003', 贷方行号: 'CR-X2', 金额: '10', 源回执号: 'RCPT-X2', 源回执时间: '2026-11-20 10:01:00', 冲销原因: '重复入账', 结算日: '2026-11-20' },
    { 借方行号: 'DBT-1002', 贷方行号: 'CR-X3', 金额: '999', 源回执号: 'RCPT-X3', 源回执时间: '2026-11-20 10:02:00', 冲销原因: '金额有误', 结算日: '2026-11-20' },
  ]
}

async function callApi(path: string, init?: RequestInit) {
  const response = await request(`${ENDPOINT}${path}`, init)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(data.detail || data.message || `接口返回 ${response.status}`)
  }
  return data
}

async function submitCreate() {
  errorMessage.value = ''
  const lines = createForm.value.行
    .filter((l: Row) => l.借方行号 || l.贷方行号)
    .map((l: Row, index: number) => ({
      行号: `L${index + 1}`,
      借方行号: l.借方行号,
      贷方行号: l.贷方行号,
      金额: Number(l.金额),
      源回执号: l.源回执号,
      源回执时间: l.源回执时间,
      冲销原因: l.冲销原因,
      结算日: l.结算日 || createForm.value.结算日,
    }))
  try {
    const data = await callApi('/batches', {
      method: 'POST',
      body: JSON.stringify({ values: { 批次号: createForm.value.批次号, 活动编号: createForm.value.活动编号, 结算日: createForm.value.结算日, 行: lines } }),
    })
    if (!data.ok) {
      throw new Error(data.message || '批次提交未生效')
    }
    noticeMessage.value = data.message || '批次已处理'
    showCreateModal.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次提交失败'
  }
}

async function selectBatch(row: Row) {
  errorMessage.value = ''
  try {
    batchDetail.value = await callApi(`/batches/${encodeURIComponent(row.批次号)}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次详情读取失败'
  }
}

function toggleFailure(item: Row) {
  const key = `${item.批次号}|${item.行号}`
  const next = new Set(selectedFailures.value)
  if (next.has(key)) {
    next.delete(key)
  } else {
    next.add(key)
  }
  selectedFailures.value = next
}

async function restartSelected() {
  errorMessage.value = ''
  noticeMessage.value = ''
  const picked = failures.value.filter((item: Row) => selectedFailures.value.has(`${item.批次号}|${item.行号}`))
  const batchNos = [...new Set(picked.map((item: Row) => item.批次号))]
  if (batchNos.length !== 1) {
    errorMessage.value = '一次只能重新开始同一批次的失败项'
    return
  }
  const fix: Record<string, Row> = {}
  for (const item of picked) {
    const settle = corrections.value[`${item.行号}@结算日`]
    const amount = corrections.value[`${item.行号}@金额`]
    const entry: Row = {}
    if (settle) entry.结算日 = settle
    if (amount) entry.金额 = Number(amount)
    if (Object.keys(entry).length) fix[item.行号] = entry
  }
  try {
    const data = await callApi('/restart', {
      method: 'POST',
      body: JSON.stringify({ values: { 批次号: batchNos[0], 行号列表: picked.map((item: Row) => item.行号), 纠正: fix } }),
    })
    if (!data.ok) {
      throw new Error(data.message || '重新开始未生效')
    }
    noticeMessage.value = data.message
    selectedFailures.value = new Set()
    corrections.value = {}
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重新开始失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const [batchPage, failurePage, martPage, ledgerPage] = await Promise.all([
      callApi('/batches?size=200'),
      callApi('/failures'),
      callApi('/mart-flows'),
      callApi('/debit-ledger'),
    ])
    batches.value = batchPage.items ?? []
    total.value = batchPage.total ?? batches.value.length
    failures.value = failurePage.items ?? []
    martFlows.value = martPage.items ?? []
    ledger.value = ledgerPage.items ?? []
    stats.value[0].value = total.value
    stats.value[1].value = batches.value.reduce((sum: number, row: Row) => sum + Number(row.可入账净额 || 0), 0)
    stats.value[2].value = failures.value.length
    if (batchDetail.value) {
      const current = batches.value.find((row: Row) => row.批次号 === batchDetail.value?.批次号)
      if (current) {
        await selectBatch(current)
      }
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量冲销数据读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.section-title {
  margin: 18px 0 8px;
  font-size: 15px;
}
.modal-card.wide {
  width: 860px;
  max-width: 92vw;
}
.batch-head-fields {
  display: flex;
  gap: 12px;
}
.batch-head-fields .modal-field {
  flex: 1;
}
.data-table input {
  width: 110px;
  padding: 4px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 12px;
}
.restart-bar {
  margin: 8px 0 0;
}
.notice-text {
  color: #067647;
}
</style>
