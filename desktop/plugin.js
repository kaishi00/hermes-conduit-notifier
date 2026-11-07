/**
 * Conduit's half for Hermes Desktop: draws the boards conduit_show keeps for a
 * Desktop chat, where the agent's reply holds ::conduit-board{id="cb-…"}.
 *
 * The plugin keeps each board on the host (room_boards.DesktopBoards) and this
 * half reads it from GET /boards/desktop. While it's loaded it says so
 * (POST /boards/desktop/seen, every ten minutes, and that it's gone when it's turned off), so the agent is asked for the
 * line only where it will be drawn; without the half, Desktop gets the board
 * as Markdown in the reply. A diff card too long to carry its lines names the
 * whole diff the host keeps (GET /boards/diffs), read when a file is opened.
 * A card's buttons open, copy or send as the user; a tap button posts the tap
 * (POST /boards/taps) and then sends the user's message about it, which the
 * plugin hands the tap to.
 *
 * Plain ESM, loaded uncompiled: jsx() calls, not JSX. Only @hermes/plugin-sdk
 * and react resolve.
 */

import { cn, host, MessageTextContent, TRANSCRIPT_DIRECTIVE_AREA } from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'conduit_push'
const BOARD_ID = /^cb-[0-9a-f]{12}$/
const SEEN_EVERY_MS = 10 * 60 * 1000
// Kinds drawn here; every other card shows its own Markdown.
const NATIVE = new Set([
  'stats', 'chart', 'comparison', 'steps', 'status', 'table', 'timeline', 'checklist', 'list', 'progress', 'callout',
  'diff', 'composed'
])
const DIFF_ID = /^cd-[0-9a-f]{16}$/

const muted = 'text-(--ui-text-tertiary)'
const frame = 'rounded-lg border border-(--ui-border) bg-(--ui-surface) p-3'

let rest = null
let os = null
const boards = new Map()
const KEPT_READS = 200

function readBoard(id) {
  if (!boards.has(id)) {
    // The oldest read goes first, so a long session keeps a few hundred.
    if (boards.size >= KEPT_READS) boards.delete(boards.keys().next().value)
    const read = rest(`/boards/desktop?id=${id}`).then(
      answer => answer.board,
      error => {
        boards.delete(id)
        throw error
      }
    )
    boards.set(id, read)
  }
  return boards.get(id)
}

function gone(error) {
  const text = String(error?.message ?? error)
  return /\b404\b|gone/i.test(text)
}

// --- the board -----------------------------------------------------------------

function Board({ id, sessionId, streaming }) {
  const [state, setState] = useState({ board: null, error: null })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let live = true
    setState({ board: null, error: null })
    readBoard(id).then(
      board => live && setState({ board, error: null }),
      error => live && setState({ board: null, error })
    )
    return () => {
      live = false
    }
  }, [id, attempt])

  if (state.error) {
    return jsxs('div', {
      className: cn('my-2 text-sm', muted),
      children: [
        gone(state.error) ? 'This board is no longer kept on the host.' : 'The board couldn’t be loaded.',
        gone(state.error)
          ? null
          : jsx('button', {
              className: 'ml-2 underline',
              type: 'button',
              onClick: () => setAttempt(attempt + 1),
              children: 'Try again'
            })
      ]
    })
  }

  if (!state.board) {
    return jsx('div', { className: cn('my-2 text-sm', muted), children: 'Loading the board…' })
  }

  return jsx(BoardView, { board: state.board, sessionId, streaming }, id)
}

function BoardView({ board, sessionId, streaming }) {
  const cards = Array.isArray(board.cards) ? board.cards : []
  const notes = Array.isArray(board.card_markdown) ? board.card_markdown : []
  const tabs = board.layout === 'tabs' && cards.length > 1
  const [tab, setTab] = useState(0)

  if (!cards.length) {
    return typeof board.markdown === 'string' ? jsx(MessageTextContent, { text: board.markdown }) : null
  }

  const shown = tabs ? [Math.min(tab, cards.length - 1)] : cards.map((_, index) => index)

  return jsxs('div', {
    className: 'my-2 flex max-w-2xl flex-col gap-2',
    children: [
      tabs
        ? jsx('div', {
            className: 'flex flex-wrap gap-1',
            role: 'tablist',
            children: cards.map((card, index) =>
              jsx(
                'button',
                {
                  'aria-selected': index === tab,
                  className: cn(
                    'rounded-md px-2 py-1 text-xs',
                    index === tab ? 'bg-(--ui-surface-active) font-medium' : cn('hover:bg-(--ui-surface)', muted)
                  ),
                  role: 'tab',
                  type: 'button',
                  onClick: () => setTab(index),
                  children: card.title || kindName(card.type)
                },
                index
              )
            )
          })
        : null,
      ...shown.map(index =>
        jsx(
          CardFrame,
          { card: cards[index], markdown: notes[index], sessionId, showTitle: !tabs },
          `${index}-${cards[index].type}`
        )
      ),
      Array.isArray(board.next) && board.next.length && !streaming
        ? jsx(FollowUps, { next: board.next, sessionId })
        : null
    ]
  })
}

function CardFrame({ card, markdown, sessionId, showTitle }) {
  if (!NATIVE.has(card.type)) {
    return typeof markdown === 'string' && markdown
      ? jsx('div', { className: frame, children: jsx(MessageTextContent, { text: markdown }) })
      : null
  }
  return jsxs('div', {
    className: frame,
    children: [
      showTitle && card.title ? jsx('div', { className: 'mb-2 text-sm font-medium', children: card.title }) : null,
      card.type === 'composed' ? jsx(Composed, { card, sessionId }) : jsx(CardBody, { card, sessionId }),
      card.type !== 'composed' && Array.isArray(card.actions) && card.actions.length
        ? jsx(Buttons, { buttons: card.actions, card, sessionId, values: {} })
        : null,
      card.source ? jsx(Source, { source: card.source }) : null
    ]
  })
}

function CardBody({ card, sessionId }) {
  switch (card.type) {
    case 'stats':
      return jsx(Stats, { stats: card.stats })
    case 'chart':
      return jsx(Chart, { chart: card.chart })
    case 'comparison':
      return jsx(Comparison, { options: card.options })
    case 'steps':
      return jsx(Steps, { needs: card.needs, steps: card.steps })
    case 'status':
      return jsx(Status, { status: card.status })
    case 'table':
      return jsx(Table, { table: card.table })
    case 'timeline':
      return jsx(Timeline, { days: card.timeline })
    case 'checklist':
      return jsx(Checklist, { card, sessionId })
    case 'list':
      return jsx(List, { items: card.list, ranked: card.ranked })
    case 'progress':
      return jsx(Progress, { items: card.progress })
    case 'callout':
      return jsx(Callout, { callout: card.callout })
    case 'diff':
      return jsx(Diff, { diff: card.diff })
    default:
      return null
  }
}

function kindName(type) {
  return (
    { comparison: 'Compare', timeline: 'Plan', checklist: 'Checklist', diff: 'Changes', composed: 'Card' }[type] ??
    (type || 'Card')
  )
}

function Source({ source }) {
  const name = source.url ? jsx(Link, { href: source.url, children: source.name }) : source.name
  return jsxs('div', { className: cn('mt-2 text-xs', muted), children: ['From ', name] })
}

function Link({ href, children }) {
  return jsx('a', {
    className: 'underline decoration-dotted underline-offset-2',
    href,
    rel: 'noreferrer',
    target: '_blank',
    onClick: event => {
      if (!os?.openExternal || !/^https?:\/\//.test(href)) return
      event.preventDefault()
      os.openExternal(href)
    },
    children
  })
}

function FollowUps({ next, sessionId }) {
  return jsx('div', {
    className: 'flex flex-wrap gap-1.5',
    children: next.map((text, index) =>
      jsx(
        'button',
        {
          className: 'rounded-full border border-(--ui-border) px-2.5 py-1 text-xs hover:bg-(--ui-surface)',
          type: 'button',
          onClick: () => {
            const sent = host.composer?.submit?.(sessionId ?? null, text)
            if (sent === false) host.notify?.({ kind: 'warning', message: 'Open that chat to send it' })
          },
          children: text
        },
        index
      )
    )
  })
}

// --- composed cards and buttons --------------------------------------------------

const toneText = {
  accent: 'text-(--ui-accent)',
  good: 'text-emerald-600 dark:text-emerald-400',
  warning: 'text-amber-600 dark:text-amber-400',
  bad: 'text-red-600 dark:text-red-400'
}

function startingValues(card) {
  const values = {}
  for (const section of card.sections ?? []) {
    for (const element of section.items ?? []) {
      if (element.kind === 'field') values[element.id] = element.value ?? (element.input === 'toggle' ? 'off' : '')
    }
  }
  return values
}

function Composed({ card, sessionId }) {
  const [values, setValues] = useState(() => startingValues(card))
  const [open, setOpen] = useState(false)
  const sections = card.sections ?? []
  const folded = sections.some(section => section.more)
  const set = (id, value) => setValues({ ...values, [id]: value })
  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      ...sections
        .filter(section => open || !section.more)
        .map((section, index) => {
          const items = section.items ?? []
          const buttons = items.filter(element => element.kind === 'button')
          return jsxs(
            'div',
            {
              className: section.layout === 'row' ? 'flex flex-wrap items-center gap-3' : 'flex flex-col gap-1.5',
              children: [
                ...items
                  .filter(element => element.kind !== 'button')
                  .map((element, number) => jsx(Element, { element, values, set }, number)),
                buttons.length ? jsx(Buttons, { buttons, card, sessionId, values }) : null
              ]
            },
            index
          )
        }),
      folded
        ? jsx('button', {
            className: cn('self-start text-xs underline', muted),
            type: 'button',
            onClick: () => setOpen(!open),
            children: open ? 'Show less' : 'Show more'
          })
        : null,
      Array.isArray(card.actions) && card.actions.length
        ? jsx(Buttons, { buttons: card.actions, card, sessionId, values })
        : null
    ]
  })
}

function Element({ element, values, set }) {
  const tone = toneText[element.tone] ?? ''
  switch (element.kind) {
    case 'text': {
      const role = {
        heading: 'text-base font-semibold',
        caption: cn('text-xs', muted),
        mono: 'font-mono text-xs whitespace-pre-wrap',
        body: 'text-sm'
      }[element.role ?? 'body']
      return jsx('div', { className: cn(role, tone), children: element.text })
    }
    case 'metric':
      return jsxs('div', {
        children: [
          element.label ? jsx('div', { className: cn('text-xs', muted), children: element.label }) : null,
          jsx('div', { className: cn('text-xl font-semibold tabular-nums', tone), children: element.value }),
          element.change ? jsx('div', { className: cn('text-xs tabular-nums', muted), children: element.change }) : null
        ]
      })
    case 'badge':
      return jsx('span', {
        className: cn('self-start rounded-full border border-(--ui-border) px-2 py-0.5 text-xs', tone),
        children: element.text
      })
    case 'icon':
      // Desktop has no SF Symbols: the symbol's name says what it was.
      return null
    case 'image':
      return jsx('img', { alt: element.label ?? '', className: 'max-h-64 rounded-md', src: element.url })
    case 'progress':
      return jsxs('div', {
        className: 'text-xs',
        children: [
          element.label ? jsx('div', { className: muted, children: element.label }) : null,
          jsx('div', {
            className: 'mt-1 h-1.5 w-full rounded-full bg-(--ui-surface-active)',
            children: jsx('div', {
              className: cn('h-1.5 rounded-full bg-current', tone || 'text-(--ui-accent)'),
              style: { width: `${Math.round(element.progress * 100)}%` }
            })
          })
        ]
      })
    case 'divider':
      return jsx('hr', { className: 'border-(--ui-border)' })
    case 'field':
      return jsx(Field, { field: element, value: values[element.id] ?? '', set })
    default:
      return null
  }
}

function Field({ field, value, set }) {
  const input = 'w-full rounded-md border border-(--ui-border) bg-transparent px-2 py-1 text-sm'
  let control
  if (field.input === 'choice') {
    control = jsx('select', {
      className: input,
      value,
      onChange: event => set(field.id, event.target.value),
      children: [
        value ? null : jsx('option', { value: '', children: '' }, ''),
        ...field.options.map(option => jsx('option', { value: option, children: option }, option))
      ]
    })
  } else if (field.input === 'toggle') {
    control = jsx('input', {
      checked: value === 'on',
      type: 'checkbox',
      onChange: event => set(field.id, event.target.checked ? 'on' : 'off')
    })
  } else if (field.input === 'text' && (field.lines ?? 1) > 1) {
    control = jsx('textarea', {
      className: input,
      placeholder: field.placeholder ?? '',
      rows: field.lines,
      value,
      onChange: event => set(field.id, event.target.value)
    })
  } else {
    const type = { date: value.includes('T') ? 'datetime-local' : 'date', number: 'number' }[field.input] ?? 'text'
    control = jsx('input', {
      className: input,
      max: field.max,
      min: field.min,
      placeholder: field.placeholder ?? '',
      type,
      value,
      onChange: event => set(field.id, event.target.value)
    })
  }
  return jsxs('label', {
    className: 'flex flex-1 flex-col gap-1 text-xs',
    children: [field.label ? jsx('span', { className: muted, children: field.label }) : null, control]
  })
}

function Buttons({ buttons, card, sessionId, values }) {
  const [busy, setBusy] = useState(null)
  const press = async button => {
    if (button.confirm && !window.confirm(button.confirm)) return
    if (button.action === 'open') {
      if (os?.openExternal) os.openExternal(button.target)
      else window.open(button.target, '_blank', 'noreferrer')
    } else if (button.action === 'copy') {
      navigator.clipboard?.writeText(button.target)
      host.notify?.({ kind: 'success', message: 'Copied' })
    } else if (button.action === 'send') {
      if (host.composer?.submit?.(sessionId ?? null, button.target) === false) {
        host.notify?.({ kind: 'warning', message: 'Open that chat to send it' })
      }
    } else if (button.action === 'tap') {
      if (!sessionId) return
      setBusy(button.id)
      try {
        await rest('/boards/taps', { method: 'POST', body: { sessions: [sessionId], card, action: button.id, values } })
        const said = card.title ? `${button.label} · ${card.title}` : button.label
        if (host.composer?.submit?.(sessionId, said) === false) {
          host.notify?.({ kind: 'warning', message: 'Open that chat to send it' })
        }
      } catch {
        host.notify?.({ kind: 'warning', message: 'That tap didn’t reach Hermes' })
      } finally {
        setBusy(null)
      }
    }
  }
  const style = {
    primary: 'bg-(--ui-accent) text-white border-transparent',
    destructive: 'text-red-600 dark:text-red-400',
    plain: ''
  }
  return jsx('div', {
    className: 'flex flex-wrap gap-1.5',
    children: buttons.map((button, index) =>
      jsx(
        'button',
        {
          className: cn(
            'rounded-md border border-(--ui-border) px-2.5 py-1 text-xs hover:opacity-90',
            style[button.style ?? 'plain']
          ),
          disabled: busy !== null || (button.action === 'tap' && !sessionId),
          type: 'button',
          onClick: () => press(button),
          children: busy === button.id ? '…' : button.label
        },
        index
      )
    )
  })
}

// --- the cards -----------------------------------------------------------------

const tones = { good: 'text-emerald-600 dark:text-emerald-400', bad: 'text-red-600 dark:text-red-400' }

function Stats({ stats }) {
  return jsx('div', {
    className: 'grid grid-cols-2 gap-3 sm:grid-cols-3',
    children: stats.map((stat, index) =>
      jsxs(
        'div',
        {
          children: [
            jsx('div', { className: cn('text-xs', muted), children: stat.label }),
            jsx('div', { className: 'text-xl font-semibold tabular-nums', children: stat.value }),
            stat.change
              ? jsx('div', { className: cn('text-xs tabular-nums', tones[stat.tone] ?? muted), children: stat.change })
              : null,
            stat.detail ? jsx('div', { className: cn('text-xs', muted), children: stat.detail }) : null
          ]
        },
        index
      )
    )
  })
}

const palette = ['var(--chart-1, #3b82f6)', 'var(--chart-2, #f59e0b)', 'var(--chart-3, #10b981)', 'var(--chart-4, #ef4444)']

function formatNumber(value) {
  return Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 4 }) : ''
}

function Chart({ chart }) {
  const labels = chart.labels ?? []
  const series = chart.series ?? []
  if (chart.kind === 'pie') {
    return jsx(Pie, { labels, values: series[0]?.values ?? [], unit: chart.unit })
  }
  const width = 480
  const height = 180
  const pad = 24
  const values = series.flatMap(line => line.values)
  const top = Math.max(0, ...values)
  const bottom = Math.min(0, ...values)
  const span = top - bottom || 1
  const y = value => pad + (height - 2 * pad) * (1 - (value - bottom) / span)
  const step = (width - 2 * pad) / Math.max(labels.length, 1)
  const bars = chart.kind === 'bar'
  const x = index => pad + step * index + (bars ? 0 : step / 2)

  const marks = series.map((line, at) => {
    const color = palette[at % palette.length]
    if (bars) {
      const barWidth = Math.max(2, (step * 0.8) / series.length)
      return line.values.map((value, index) =>
        jsx(
          'rect',
          {
            fill: color,
            height: Math.abs(y(value) - y(0)),
            rx: 2,
            width: barWidth,
            x: x(index) + step * 0.1 + barWidth * at,
            y: Math.min(y(value), y(0)),
            children: jsx('title', { children: `${labels[index]}: ${formatNumber(value)}` })
          },
          `${at}-${index}`
        )
      )
    }
    const points = line.values.map((value, index) => `${x(index)},${y(value)}`).join(' ')
    return jsxs(
      'g',
      {
        children: [
          chart.kind === 'area'
            ? jsx('polygon', {
                fill: color,
                opacity: 0.18,
                points: `${x(0)},${y(0)} ${points} ${x(line.values.length - 1)},${y(0)}`
              })
            : null,
          jsx('polyline', { fill: 'none', points, stroke: color, strokeWidth: 2 })
        ]
      },
      at
    )
  })

  const every = Math.ceil(labels.length / 8)
  return jsxs('div', {
    children: [
      jsxs('svg', {
        className: 'w-full',
        role: 'img',
        'aria-label': `${chart.kind} chart`,
        viewBox: `0 0 ${width} ${height}`,
        children: [
          jsx('line', { stroke: 'currentColor', opacity: 0.2, x1: pad, x2: width - pad, y1: y(0), y2: y(0) }),
          jsx('text', { className: 'fill-current text-[10px]', opacity: 0.6, x: 0, y: pad, children: formatNumber(top) }),
          ...marks.flat(),
          ...labels.map((label, index) =>
            index % every
              ? null
              : jsx(
                  'text',
                  {
                    className: 'fill-current text-[10px]',
                    opacity: 0.6,
                    textAnchor: 'middle',
                    x: x(index) + (bars ? step / 2 : 0),
                    y: height - 6,
                    children: label
                  },
                  `l${index}`
                )
          )
        ]
      }),
      series.length > 1 || chart.unit ? jsx(Legend, { names: series.map(line => line.name ?? 'Value'), unit: chart.unit }) : null
    ]
  })
}

function Legend({ names, unit }) {
  return jsxs('div', {
    className: cn('mt-1 flex flex-wrap gap-3 text-xs', muted),
    children: [
      ...names.map((name, index) =>
        jsxs(
          'span',
          {
            className: 'inline-flex items-center gap-1',
            children: [
              jsx('span', { className: 'inline-block size-2 rounded-full', style: { background: palette[index % palette.length] } }),
              name
            ]
          },
          index
        )
      ),
      unit ? jsx('span', { children: unit }) : null
    ]
  })
}

function Pie({ labels, values, unit }) {
  const total = values.reduce((sum, value) => sum + Math.max(value, 0), 0) || 1
  return jsx('div', {
    className: 'flex flex-col gap-1.5',
    children: labels.map((label, index) => {
      const share = Math.max(values[index] ?? 0, 0) / total
      return jsxs(
        'div',
        {
          className: 'text-sm',
          children: [
            jsxs('div', {
              className: 'flex justify-between',
              children: [
                jsx('span', { children: label }),
                jsx('span', {
                  className: cn('tabular-nums', muted),
                  children: `${formatNumber(values[index])}${unit ? ` ${unit}` : ''} · ${Math.round(share * 100)}%`
                })
              ]
            }),
            jsx(Bar, { color: palette[index % palette.length], share })
          ]
        },
        index
      )
    })
  })
}

function Bar({ share, color }) {
  return jsx('div', {
    className: 'mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-(--ui-surface-active)',
    children: jsx('div', {
      className: 'h-full rounded-full',
      style: { background: color ?? 'var(--accent, currentColor)', width: `${Math.round(Math.min(share, 1) * 100)}%` }
    })
  })
}

function Comparison({ options }) {
  return jsx('div', {
    className: 'grid gap-2 sm:grid-cols-2',
    children: options.map((option, index) =>
      jsxs(
        'div',
        {
          className: cn('rounded-md border p-2 text-sm', option.recommended ? 'border-(--accent)' : 'border-(--ui-border)'),
          children: [
            jsxs('div', {
              className: 'font-medium',
              children: [option.name, option.recommended ? jsx('span', { className: cn('ml-1 text-xs', muted), children: '· Recommended' }) : null]
            }),
            option.summary ? jsx('div', { className: muted, children: option.summary }) : null,
            ...(option.facts ?? []).map((fact, at) =>
              jsxs(
                'div',
                {
                  className: 'flex justify-between gap-2',
                  children: [jsx('span', { className: muted, children: fact.label }), jsx('span', { children: fact.value })]
                },
                `f${at}`
              )
            ),
            ...(option.pros ?? []).map((pro, at) => jsx('div', { children: `+ ${pro}` }, `p${at}`)),
            ...(option.cons ?? []).map((con, at) => jsx('div', { className: muted, children: `− ${con}` }, `c${at}`))
          ]
        },
        index
      )
    )
  })
}

function minutes(seconds) {
  const hours = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  const secs = seconds % 60
  return [hours && `${hours} h`, mins && `${mins} min`, secs && `${secs} s`].filter(Boolean).join(' ')
}

function Steps({ needs, steps }) {
  return jsxs('div', {
    className: 'text-sm',
    children: [
      needs?.length
        ? jsx('ul', { className: cn('mb-2 list-disc pl-5', muted), children: needs.map((need, index) => jsx('li', { children: need }, index)) })
        : null,
      jsx('ol', {
        className: 'flex list-decimal flex-col gap-1 pl-5',
        children: steps.map((step, index) =>
          jsxs(
            'li',
            {
              children: [step.text, step.timer ? jsx('span', { className: cn('ml-1', muted), children: `(${minutes(step.timer)})` }) : null]
            },
            index
          )
        )
      })
    ]
  })
}

const phases = { waiting: 'Not started', done: 'Done', problem: 'Problem' }

function Status({ status }) {
  const done = status.phase === 'done'
  const share = done ? 1 : status.progress
  return jsxs('div', {
    className: 'text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2',
        children: [
          jsx('span', { className: cn('font-medium', status.phase === 'problem' && tones.bad), children: status.state }),
          jsx('span', {
            className: cn('text-xs', muted),
            children: [phases[status.phase], status.eta].filter(Boolean).join(' · ')
          })
        ]
      }),
      typeof share === 'number' ? jsx(Bar, { share }) : null,
      status.detail ? jsx('div', { className: cn('mt-1', muted), children: status.detail }) : null,
      status.milestones?.length
        ? jsx('div', {
            className: cn('mt-1 text-xs', muted),
            children: status.milestones.map(stage => `${stage.done ? '✓ ' : ''}${stage.label}`).join(' → ')
          })
        : null
    ]
  })
}

function sortKey(cell) {
  const number = Number(String(cell).replace(/[^0-9.\-]/g, ''))
  return /\d/.test(String(cell)) && Number.isFinite(number) ? number : String(cell).toLowerCase()
}

function Table({ table }) {
  const [order, setOrder] = useState(null)
  const rows = useMemo(() => {
    if (!order) return table.rows
    const sorted = [...table.rows].sort((a, b) => {
      const left = sortKey(a[order.column] ?? '')
      const right = sortKey(b[order.column] ?? '')
      return left < right ? -1 : left > right ? 1 : 0
    })
    return order.down ? sorted.reverse() : sorted
  }, [table.rows, order])

  return jsx('div', {
    className: 'overflow-x-auto',
    children: jsxs('table', {
      className: 'w-full text-sm',
      children: [
        jsx('thead', {
          children: jsx('tr', {
            children: table.columns.map((name, column) =>
              jsx(
                'th',
                {
                  className: cn('cursor-pointer select-none border-b border-(--ui-border) px-2 py-1 text-left font-medium', muted),
                  onClick: () =>
                    setOrder(order?.column === column ? { column, down: !order.down } : { column, down: false }),
                  children: `${name}${order?.column === column ? (order.down ? ' ↓' : ' ↑') : ''}`
                },
                column
              )
            )
          })
        }),
        jsx('tbody', {
          children: rows.map((row, index) =>
            jsx(
              'tr',
              {
                className: 'border-b border-(--ui-border) last:border-0',
                children: row.map((cell, column) => jsx('td', { className: 'px-2 py-1 tabular-nums', children: cell }, column))
              },
              index
            )
          )
        })
      ]
    })
  })
}

function Timeline({ days }) {
  return jsx('div', {
    className: 'flex flex-col gap-3 text-sm',
    children: days.map((day, index) =>
      jsxs(
        'div',
        {
          children: [
            jsxs('div', {
              className: 'font-medium',
              children: [day.label, day.title ? jsx('span', { className: cn('font-normal', muted), children: ` · ${day.title}` }) : null]
            }),
            jsx('ul', {
              className: 'mt-1 flex flex-col gap-1 border-l border-(--ui-border) pl-3',
              children: day.stops.map((stop, at) =>
                jsxs(
                  'li',
                  {
                    children: [
                      stop.time ? jsx('span', { className: cn('mr-1.5 tabular-nums', muted), children: stop.time }) : null,
                      stop.text,
                      stop.place
                        ? jsxs('span', {
                            className: muted,
                            children: [' · ', jsx(Link, { href: `https://maps.apple.com/?q=${encodeURIComponent(stop.place)}`, children: stop.place })]
                          })
                        : null,
                      stop.note ? jsx('div', { className: cn('text-xs italic', muted), children: stop.note }) : null
                    ]
                  },
                  at
                )
              )
            })
          ]
        },
        index
      )
    )
  })
}

function checklistKey(card) {
  return (card.live || card.title || 'checklist').trim().toLowerCase()
}

function Checklist({ card, sessionId }) {
  const items = card.checklist.map(item => item.text)
  const key = checklistKey(card)
  const [ticked, setTicked] = useState([])

  useEffect(() => {
    if (!sessionId) return undefined
    let live = true
    rest(`/boards/checklists?session=${encodeURIComponent(sessionId)}`).then(
      answer => live && setTicked(answer?.ticks?.[key] ?? []),
      () => {}
    )
    return () => {
      live = false
    }
  }, [sessionId, key])

  const done = new Set(ticked.map(text => text.toLowerCase()))
  const tick = (item, on) => {
    setTicked(on ? [...ticked, item] : ticked.filter(text => text.toLowerCase() !== item.toLowerCase()))
    if (!sessionId) return
    rest('/boards/checklists', {
      method: 'POST',
      body: { sessions: [sessionId], key, title: card.title ?? '', items, change: { item, ticked: on } }
    }).then(
      answer => setTicked(answer?.ticks?.[key] ?? []),
      () => host.notify?.({ kind: 'warning', message: 'That tick wasn’t saved' })
    )
  }

  let group
  return jsx('div', {
    className: 'flex flex-col gap-1 text-sm',
    children: card.checklist.flatMap((item, index) => {
      const rows = []
      if (item.group && item.group !== group) {
        rows.push(jsx('div', { className: cn('mt-1 text-xs font-medium', muted), children: item.group }, `g${index}`))
      }
      group = item.group
      const on = done.has(item.text.toLowerCase())
      rows.push(
        jsxs(
          'label',
          {
            className: 'flex cursor-pointer items-start gap-2',
            children: [
              jsx('input', {
                checked: on,
                className: 'mt-0.5',
                disabled: !sessionId,
                type: 'checkbox',
                onChange: event => tick(item.text, event.target.checked)
              }),
              jsx('span', { className: on ? cn('line-through', muted) : undefined, children: item.text })
            ]
          },
          index
        )
      )
      return rows
    })
  })
}

function List({ items, ranked }) {
  return jsx(ranked ? 'ol' : 'ul', {
    className: 'flex flex-col gap-1.5 text-sm',
    children: items.map((item, index) =>
      jsxs(
        'li',
        {
          className: 'flex items-start gap-2',
          children: [
            ranked ? jsx('span', { className: cn('w-5 shrink-0 tabular-nums', muted), children: `${index + 1}.` }) : null,
            jsxs('div', {
              className: 'min-w-0 flex-1',
              children: [
                jsx('div', { className: 'font-medium', children: item.url ? jsx(Link, { href: item.url, children: item.title }) : item.title }),
                item.detail ? jsx('div', { className: muted, children: item.detail }) : null
              ]
            }),
            item.value ? jsx('span', { className: 'shrink-0 tabular-nums', children: item.value }) : null
          ]
        },
        index
      )
    )
  })
}

function Progress({ items }) {
  return jsx('div', {
    className: 'flex flex-col gap-2 text-sm',
    children: items.map((item, index) => {
      const share = item.goal > 0 ? item.value / item.goal : 0
      const unit = item.unit ? ` ${item.unit}` : ''
      return jsxs(
        'div',
        {
          children: [
            jsxs('div', {
              className: 'flex justify-between gap-2',
              children: [
                jsx('span', { children: item.label }),
                jsx('span', {
                  className: cn('tabular-nums', muted),
                  children: `${formatNumber(item.value)} of ${formatNumber(item.goal)}${unit} · ${Math.round(Math.min(share, 1) * 100)}%`
                })
              ]
            }),
            jsx(Bar, { share })
          ]
        },
        index
      )
    })
  })
}

const calloutNames = { tip: 'Tip', note: 'Note', warning: 'Warning' }

function Callout({ callout }) {
  const tone = callout.tone ?? 'note'
  return jsxs('div', {
    className: cn('border-l-2 pl-2 text-sm', tone === 'warning' ? 'border-amber-500' : 'border-(--accent)'),
    children: [jsx('span', { className: 'font-medium', children: `${calloutNames[tone] ?? 'Note'}: ` }), callout.text]
  })
}

// A diff: its files, each folding open to its hunks with line numbers. A diff
// too long for the card lists its files, and the whole one is read from the
// host (GET /boards/diffs) when the first file is opened.

const diffs = new Map()

function readDiff(id) {
  if (!diffs.has(id)) {
    if (diffs.size >= KEPT_READS) diffs.delete(diffs.keys().next().value)
    const read = rest(`/boards/diffs?id=${id}`).then(
      answer => answer.diff,
      error => {
        diffs.delete(id)
        throw error
      }
    )
    diffs.set(id, read)
  }
  return diffs.get(id)
}

const statusNames = { added: 'new', deleted: 'deleted', renamed: 'renamed' }

function plural(count, one) {
  return `${count.toLocaleString()} ${one}${count === 1 ? '' : 's'}`
}

function Diff({ diff }) {
  const files = Array.isArray(diff.files) ? diff.files : []
  const whole = files.some(file => Array.isArray(file.hunks))
  const [state, setState] = useState({ diff: null, error: null })
  // Each time a file is opened; a failed read is tried again.
  const [wanted, setWanted] = useState(0)

  useEffect(() => {
    if (whole || !wanted || !DIFF_ID.test(diff.id ?? '')) return
    let live = true
    readDiff(diff.id).then(
      read => live && setState({ diff: read, error: null }),
      error => live && setState({ diff: null, error })
    )
    return () => {
      live = false
    }
  }, [whole, wanted, diff.id])

  const shown = whole ? files : (state.diff?.files ?? files)
  const count = files.length + (diff.more_files ?? 0)
  return jsxs('div', {
    className: 'flex flex-col gap-1 text-sm',
    children: [
      jsxs('div', {
        className: muted,
        children: [
          plural(count, 'file'),
          ' · ',
          jsx('span', { className: tones.good, children: `+${(diff.added ?? 0).toLocaleString()}` }),
          ' ',
          jsx('span', { className: tones.bad, children: `\u2212${(diff.removed ?? 0).toLocaleString()}` })
        ]
      }),
      ...shown.map((file, index) =>
        jsx(
          DiffFile,
          {
            file,
            open: shown.length === 1 && !file.binary,
            onOpen: () => {
              setState(now => (now.error ? { diff: null, error: null } : now))
              setWanted(count => count + 1)
            },
            loading: !whole && !state.diff && !state.error && DIFF_ID.test(diff.id ?? ''),
            failed: !whole && state.error ? gone(state.error) ? 'This diff is no longer kept on the host.' : 'The diff couldn’t be loaded.' : null
          },
          `${index}-${file.path}`
        )
      ),
      diff.more_files ? jsx('div', { className: muted, children: `and ${plural(diff.more_files, 'more file')}` }) : null
    ]
  })
}

function DiffFile({ file, open, onOpen, loading, failed }) {
  const name = file.status === 'renamed' && file.old_path ? `${file.old_path} \u2192 ${file.path}` : file.path
  const note = statusNames[file.status]
  const hunks = Array.isArray(file.hunks) ? file.hunks : null
  return jsxs('details', {
    className: 'rounded-md border border-(--ui-border)',
    open,
    onToggle: event => event.currentTarget.open && onOpen(),
    children: [
      jsxs('summary', {
        className: 'flex cursor-pointer items-baseline gap-2 px-2 py-1',
        children: [
          jsx('span', { className: 'min-w-0 flex-1 truncate font-mono text-xs', title: name, children: name }),
          note ? jsx('span', { className: cn('text-xs', muted), children: note }) : null,
          file.binary
            ? jsx('span', { className: cn('text-xs', muted), children: 'binary' })
            : jsxs('span', {
                className: 'text-xs tabular-nums',
                children: [
                  jsx('span', { className: tones.good, children: `+${file.added}` }),
                  ' ',
                  jsx('span', { className: tones.bad, children: `\u2212${file.removed}` })
                ]
              })
        ]
      }),
      file.binary
        ? jsx('div', { className: cn('px-2 pb-2 text-xs', muted), children: 'A binary file: no lines to show.' })
        : hunks
          ? jsxs('div', {
              className: 'overflow-x-auto border-t border-(--ui-border)',
              children: [
                ...hunks.map((hunk, index) => jsx(DiffHunk, { hunk }, index)),
                file.cut ? jsx('div', { className: cn('px-2 py-1 text-xs', muted), children: 'The rest of this file’s changes aren’t shown.' }) : null
              ]
            })
          : jsx('div', {
              className: cn('px-2 pb-2 text-xs', muted),
              children: failed ?? (loading ? 'Loading the diff…'
                : file.added || file.removed ? 'Its lines weren’t kept on Hermes.' : 'No lines changed.')
            })
    ]
  })
}

function DiffHunk({ hunk }) {
  let old = hunk.old_start
  let now = hunk.new_start
  const rows = hunk.lines.map(line => {
    const kind = line[0]
    const row = { kind, text: line.slice(1), old: kind === '+' ? '' : old, now: kind === '-' ? '' : now }
    if (kind !== '+') old += 1
    if (kind !== '-') now += 1
    return row
  })
  const tint = { '+': 'bg-emerald-500/10', '-': 'bg-red-500/10' }
  return jsxs('div', {
    className: 'font-mono text-xs',
    children: [
      jsx('div', {
        className: cn('bg-(--ui-surface-active) px-2 py-0.5', muted),
        children: `@@ -${hunk.old_start} +${hunk.new_start} @@${hunk.context ? ` ${hunk.context}` : ''}`
      }),
      jsx('table', {
        className: 'w-full border-collapse',
        children: jsx('tbody', {
          children: rows.map((row, index) =>
            jsxs(
              'tr',
              {
                className: tint[row.kind] ?? '',
                children: [
                  jsx('td', { className: cn('select-none px-1 text-right tabular-nums', muted), children: row.old }),
                  jsx('td', { className: cn('select-none px-1 text-right tabular-nums', muted), children: row.now }),
                  jsx('td', { className: cn('select-none px-1', row.kind === ' ' ? muted : ''), children: row.kind }),
                  jsx('td', { className: 'whitespace-pre pr-2', children: row.text })
                ]
              },
              index
            )
          )
        })
      })
    ]
  })
}

// --- the plugin ----------------------------------------------------------------

export default {
  id: ID,
  name: 'Conduit boards',
  register(ctx) {
    rest = ctx.rest
    os = ctx.os
    const seen = () => ctx.rest('/boards/desktop/seen', { method: 'POST', body: {} }).catch(() => {})
    seen()
    ctx.setInterval(seen, SEEN_EVERY_MS)
    ctx.onDispose(() => {
      boards.clear()
      diffs.clear()
      // Turned off: the agent stops writing a line nothing would draw.
      ctx.rest('/boards/desktop/seen', { method: 'POST', body: { gone: true } }).catch(() => {})
    })

    ctx.register({
      id: 'board',
      area: TRANSCRIPT_DIRECTIVE_AREA,
      data: {
        name: 'conduit-board',
        render: ({ attrs, sessionId, streaming }) =>
          BOARD_ID.test(attrs?.id ?? '') ? jsx(Board, { id: attrs.id, sessionId, streaming }) : null
      }
    })
  }
}
