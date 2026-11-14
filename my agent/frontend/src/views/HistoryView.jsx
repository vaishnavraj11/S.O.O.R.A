import React, { useState, useEffect } from 'react'
import {
  IconPlay,
  IconCheckCircle,
  IconAlertTriangle,
  IconFileText,
} from '../components/icons'

export default function HistoryView({ taskResult, apiBaseUrl, onSelectTask, onPlaySound }) {
  const [tasks, setTasks] = useState(taskResult?.task_id ? [taskResult] : [])

  useEffect(() => {
    let cancelled = false
    const fetchHistory = async () => {
      if (!apiBaseUrl) return
      try {
        const response = await fetch(`${apiBaseUrl}/api/tasks?limit=50`)
        if (!response.ok) return
        const data = await response.json()
        const backendTasks = data.tasks || []
        if (!cancelled && backendTasks.length > 0) {
          // Merge with current taskResult if in memory
          const map = new Map()
          if (taskResult?.task_id) {
            map.set(taskResult.task_id, taskResult)
          }
          backendTasks.forEach(t => {
            if (!map.has(t.task_id)) {
              map.set(t.task_id, t)
            }
          })
          setTasks(Array.from(map.values()))
        }
      } catch (err) {
        // Fallback to active task
      }
    }
    fetchHistory()
    return () => { cancelled = true }
  }, [apiBaseUrl, taskResult])

  return (
    <div className="space-y-6 pb-12">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-[#FF6A00]" />
            <span className="text-xs font-semibold tracking-wider text-[#FF8A00] uppercase font-mono">
              Task Archive & Timeline
            </span>
          </div>
          <h1 className="text-2xl font-bold text-white tracking-tight mt-1">
            Execution History
          </h1>
        </div>

        <div className="text-xs font-mono text-zinc-400 bg-black/40 px-3 py-1.5 rounded-xl border border-white/10">
          Active Session Tasks: <strong className="text-white">{tasks.length}</strong>
        </div>
      </div>

      <div className="space-y-4">
        {tasks.length === 0 ? (
          <section className="glass-panel p-6 rounded-2xl border border-white/10">
            <h2 className="text-base font-bold text-white">No task history loaded</h2>
            <p className="text-sm text-zinc-400 mt-2">
              Run a backend task to add a real execution record to this session.
            </p>
          </section>
        ) : tasks.map((task) => {
          const isDone = task.verdict === 'DONE' || task.status === 'COMPLETED'

          return (
            <article
              key={task.task_id}
              className="glass-panel p-5 rounded-2xl border border-white/10 hover:border-[#FF6A00]/40 transition-all group"
            >
              <div className="flex flex-wrap items-center justify-between gap-3 pb-3 mb-3 border-b border-white/5">
                <div className="flex items-center gap-3">
                  <span className="font-mono text-xs font-bold text-[#FF8A00]">
                    {task.task_id}
                  </span>
                  <span className="text-xs text-zinc-500 font-mono hidden sm:inline">
                    {task.updated_at || task.created_at}
                  </span>
                </div>

                <div className="flex items-center gap-2">
                  <span className={`px-2.5 py-0.5 rounded-full text-xs font-mono font-bold flex items-center gap-1.5 ${
                    isDone
                      ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                      : 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
                  }`}>
                    {isDone ? <IconCheckCircle className="w-3.5 h-3.5" /> : <IconAlertTriangle className="w-3.5 h-3.5" />}
                    {task.verdict || task.status}
                  </span>
                </div>
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-center">
                <div className="lg:col-span-8 space-y-1.5">
                  <h3 className="text-base font-bold text-white group-hover:text-[#FF8A00] transition-colors">
                    {task.user_request}
                  </h3>
                  <p className="text-xs text-zinc-400 leading-relaxed font-sans line-clamp-2">
                    {task.final_report || task.error || 'Task state captured from the backend response.'}
                  </p>

                  <div className="flex flex-wrap items-center gap-4 text-xs font-mono text-zinc-500 pt-1">
                    <span>Status: <strong className="text-zinc-300">{task.status}</strong></span>
                    <span>Steps: <strong className="text-zinc-300">{task.completed_steps || 0}/{task.total_steps || 0}</strong></span>
                    <span>Evidence: <strong className="text-emerald-400">{task.evidence?.length || 0} items</strong></span>
                  </div>
                </div>

                <div className="lg:col-span-4 flex flex-wrap lg:justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      onPlaySound?.('click')
                      onSelectTask?.(task)
                    }}
                    className="btn-primary text-xs py-2 px-3"
                  >
                    <IconPlay className="w-3.5 h-3.5" />
                    Replay Execution
                  </button>

                  <a
                    href={`${apiBaseUrl}/api/tasks/${task.task_id}/report/download?format=pdf`}
                    target="_blank"
                    rel="noopener noreferrer"
                    download
                    onClick={(e) => {
                      e.stopPropagation()
                      onPlaySound?.('click')
                    }}
                    className="btn-secondary text-xs py-2 px-3 flex items-center gap-1.5 hover:border-[#FF6A00]/50"
                  >
                    <IconFileText className="w-3.5 h-3.5 text-[#FF6A00]" />
                    <span>Download PDF</span>
                  </a>

                  <a
                    href={`${apiBaseUrl}/api/tasks/${task.task_id}/report/download?format=docx`}
                    target="_blank"
                    rel="noopener noreferrer"
                    download
                    onClick={(e) => {
                      e.stopPropagation()
                      onPlaySound?.('click')
                    }}
                    className="px-2.5 py-2 rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-xs font-mono text-zinc-400 hover:text-white transition-all flex items-center gap-1.5"
                    title="Download Word Document"
                  >
                    <span>.DOCX</span>
                  </a>

                  <button
                    type="button"
                    onClick={() => {
                      onPlaySound?.('hover')
                      onSelectTask?.(task)
                    }}
                    className="px-3 py-2 rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-xs font-mono text-zinc-300 hover:text-white transition-all flex items-center gap-1.5"
                  >
                    View Details
                  </button>
                </div>
              </div>
            </article>
          )
        })}
      </div>
    </div>
  )
}
