import React, { useState } from 'react'
import {
  IconSearch,
  IconBell,
  IconUser,
  IconVolume2,
  IconVolumeX,
  IconSparkles,
  IconShieldCheck,
  IconAlertTriangle,
  IconCheck
} from './icons'

export default function HeaderTopBar({
  onOpenSearch,
  soundEnabled,
  setSoundEnabled,
  onPlaySound,
  onOpenProfile,
  pendingNotificationsCount = 2
}) {
  const [showNotifications, setShowNotifications] = useState(false)

  const notifications = [
    { id: 1, title: 'Autonomous Recovery Logged', text: 'Scraper failed on NAM mirror -> switched to NIST endpoint and recovered in 4.1s.', time: '2m ago', type: 'recovery' },
    { id: 2, title: 'Evidence Cryptographically Sealed', text: '24 claims validated against FDA and WHO data registries.', time: '5m ago', type: 'success' },
    { id: 3, title: 'DAG Pipeline Ready', text: 'All 12 agent workers online and listening for tasks.', time: '12m ago', type: 'info' },
  ]

  return (
    <header className="h-16 border-b border-white/10 bg-[#070707]/90 backdrop-blur-md px-6 flex items-center justify-between sticky top-0 z-20 select-none">
      {/* Left: Brand Tagline */}
      <div className="flex items-center gap-4">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-sm font-extrabold tracking-wider text-white">
            TaskFinisher AI
          </span>
          <span className="hidden sm:inline text-xs text-[#A1A1AA] font-normal border-l border-white/10 pl-3">
            "Give it a job. It proves it finished."
          </span>
        </div>
      </div>

      {/* Center: Global Search Bar */}
      <div className="flex-1 max-w-md mx-6">
        <button
          type="button"
          onClick={() => {
            onPlaySound?.('click')
            onOpenSearch?.()
          }}
          className="w-full bg-[#121212] hover:bg-[#181818] border border-white/10 hover:border-[#FF6A00]/40 rounded-xl px-3.5 py-1.5 flex items-center justify-between text-xs text-zinc-400 transition-all shadow-inner group"
        >
          <div className="flex items-center gap-2.5">
            <IconSearch className="w-3.5 h-3.5 text-zinc-500 group-hover:text-[#FF8A00] transition-colors" />
            <span className="group-hover:text-zinc-300">Search tasks, evidence, sources, tools...</span>
          </div>
          <kbd className="font-mono text-[10px] bg-white/5 px-2 py-0.5 rounded border border-white/10 text-zinc-400 group-hover:border-zinc-500">
            Ctrl + K
          </kbd>
        </button>
      </div>

      {/* Right: Agent Status & Controls */}
      <div className="flex items-center gap-4">
        {/* Agent Status Pill with subtle orange pulse */}
        <div className="hidden lg:flex items-center gap-2 px-3 py-1.5 rounded-full bg-[#FF6A00]/10 border border-[#FF6A00]/30 shadow-[0_0_12px_rgba(255,106,0,0.15)]">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#FF8A00] opacity-80" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-[#FF6A00]" />
          </span>
          <span className="text-xs font-mono font-bold text-white tracking-wide">
            12 Agents Online
          </span>
        </div>

        {/* Audio FX Toggle */}
        <button
          type="button"
          onClick={() => {
            const next = !soundEnabled
            setSoundEnabled(next)
            if (next) onPlaySound?.('click', true)
          }}
          title={soundEnabled ? 'Disable Mission Control SFX' : 'Enable Mission Control SFX'}
          className={`p-2 rounded-lg border transition-all ${soundEnabled ? 'border-[#FF6A00]/40 bg-[#FF6A00]/10 text-[#FF8A00]' : 'border-white/5 text-zinc-500 hover:text-zinc-300'}`}
        >
          {soundEnabled ? <IconVolume2 className="w-4 h-4" /> : <IconVolumeX className="w-4 h-4" />}
        </button>

        {/* Notifications Drawer Toggle */}
        <div className="relative">
          <button
            type="button"
            onClick={() => {
              onPlaySound?.('click')
              setShowNotifications(!showNotifications)
            }}
            className="relative p-2 rounded-lg border border-white/5 hover:border-white/20 text-zinc-400 hover:text-white transition-all bg-white/[0.02]"
          >
            <IconBell className="w-4 h-4" />
            {pendingNotificationsCount > 0 && (
              <span className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-[#FF6A00] text-[9px] font-bold font-mono text-white flex items-center justify-center shadow-[0_0_8px_#FF6A00]">
                {pendingNotificationsCount}
              </span>
            )}
          </button>

          {/* Notifications Dropdown Panel */}
          {showNotifications && (
            <div className="absolute right-0 mt-2 w-80 glass-panel-elevated p-4 rounded-xl border border-white/10 shadow-2xl z-50 animate-in fade-in zoom-in-95 duration-150">
              <div className="flex items-center justify-between pb-2.5 mb-2.5 border-b border-white/5">
                <div className="flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full bg-[#FF8A00]" />
                  <span className="text-xs font-mono font-bold text-white uppercase tracking-wider">
                    Mission Control Alerts
                  </span>
                </div>
                <span className="text-[10px] text-zinc-500 font-mono">Live Sync</span>
              </div>

              <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                {notifications.map((notif) => (
                  <div key={notif.id} className="p-2 rounded-lg bg-black/40 border border-white/5 text-xs">
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-bold text-zinc-200 text-[11px] truncate">{notif.title}</span>
                      <span className="text-[10px] text-zinc-500 font-mono">{notif.time}</span>
                    </div>
                    <p className="text-[11px] text-zinc-400 leading-relaxed">{notif.text}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* User Profile */}
        <button
          type="button"
          onClick={() => {
            onPlaySound?.('click')
            onOpenProfile?.()
          }}
          className="flex items-center gap-2.5 pl-2 pr-3 py-1 rounded-xl bg-white/[0.03] hover:bg-white/[0.07] border border-white/10 transition-all"
        >
          <div className="w-7 h-7 rounded-lg bg-gradient-to-tr from-[#FF6A00] to-[#FF8A00] flex items-center justify-center text-white font-bold text-xs font-mono shadow-[0_0_10px_rgba(255,106,0,0.3)]">
            S
          </div>
          <div className="text-left hidden md:block">
            <div className="text-xs font-bold text-white leading-tight">Mohamed Sameer</div>
            <div className="text-[9px] text-[#FF8A00] font-mono leading-none">Mission Director</div>
          </div>
        </button>
      </div>
    </header>
  )
}
