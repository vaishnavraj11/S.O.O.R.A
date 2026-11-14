import React, { useMemo, useState } from 'react'

function TaskResults({ result, onHumanDecision, apiBaseUrl }) {
  const steps = result.steps || {}
  const stepList = Object.values(steps)
  const evidence = result.evidence || []
  const events = result.events || []
  const adkTrace = result.adk_trace || []
  const progress = Number(result.completion_percentage || 0)
  const [selectedStepId, setSelectedStepId] = useState(stepList[0]?.step_id || null)

  const selectedStep = useMemo(
    () => steps[selectedStepId] || stepList[0],
    [selectedStepId, steps, stepList],
  )

  const verdictClass = {
    DONE: 'success',
    PARTIALLY_DONE: 'warning',
    FAILED: 'danger',
    WAITING_FOR_HUMAN: 'warning',
  }[result.verdict] || 'neutral'
  const reportDownloadUrl = result.task_id && result.final_report
    ? `${apiBaseUrl}/api/tasks/${result.task_id}/report/download`
    : null
  const importantPointsDownloadUrl = result.task_id && result.final_report
    ? `${apiBaseUrl}/api/tasks/${result.task_id}/important-points/download`
    : null

  return (
    <article className="result-panel">
      <div className="result-header">
        <div>
          <p className="section-kicker">Execution result</p>
          <h2>{result.verdict}</h2>
        </div>
        <span className={`verdict ${verdictClass}`}>{result.status}</span>
      </div>

      {result.requires_human_approval && (
        <div className="approval-box blocking">
          <strong>Human decision required</strong>
          <p>{result.human_decision_point}</p>
          <div className="approval-actions">
            {(result.human_options || []).map((option) => (
              <button type="button" key={option} onClick={() => onHumanDecision(option)}>
                {option}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="progress-block">
        <div className="progress-copy">
          <span>Progress</span>
          <strong>
            {result.completed_steps}/{result.total_steps} steps
          </strong>
        </div>
        <div className="progress-track">
          <div style={{ width: `${Math.min(progress, 100)}%` }} />
        </div>
        <span className="progress-percent">{progress.toFixed(1)}%</span>
      </div>

      <div className="detail-grid">
        <div>
          <span>Task ID</span>
          <strong>{result.task_id}</strong>
        </div>
        <div>
          <span>Evidence</span>
          <strong>{evidence.length} audit item(s)</strong>
        </div>
      </div>

      <section className="result-section">
        <h3>Execution Timeline</h3>
        <div className="timeline-list">
          {stepList.map((step) => (
            <button
              type="button"
              key={step.step_id}
              className={selectedStep?.step_id === step.step_id ? 'active' : ''}
              onClick={() => setSelectedStepId(step.step_id)}
            >
              <span className={`status-dot ${step.status.toLowerCase()}`} />
              <span>
                <strong>{step.step_id}</strong>
                {step.description}
              </span>
              <em>{step.verification_status}</em>
            </button>
          ))}
        </div>
      </section>

      {selectedStep && (
        <section className="step-detail-panel">
          <div className="result-header compact">
            <div>
              <p className="section-kicker">Step detail</p>
              <h3>{selectedStep.step_id} - {selectedStep.description}</h3>
            </div>
            <span className="mode-badge">{selectedStep.agent}</span>
          </div>
          <dl className="step-dl">
            <div>
              <dt>Status</dt>
              <dd>{selectedStep.status}</dd>
            </div>
            <div>
              <dt>Attempts</dt>
              <dd>{selectedStep.attempts}</dd>
            </div>
            <div>
              <dt>Tools</dt>
              <dd>{selectedStep.tool_used?.length ? selectedStep.tool_used.join(', ') : 'No external tool'}</dd>
            </div>
            <div>
              <dt>Verification</dt>
              <dd>{selectedStep.verification_status}</dd>
            </div>
          </dl>
          <p className="step-summary">{selectedStep.output_summary || selectedStep.verification_reason}</p>
          {selectedStep.error && <p className="step-error">Error: {selectedStep.error}</p>}
          {selectedStep.recovery_action && <p className="recovery-note">Recovery: {selectedStep.recovery_action}</p>}
        </section>
      )}

      <section className="result-section">
        <h3>Evidence Trail</h3>
        {evidence.length === 0 ? (
          <p className="muted-text">No evidence available.</p>
        ) : (
          <div className="evidence-list">
            {evidence.map((item, index) => (
              <div key={item.id || `${item.content}-${index}`}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <p>{item.content || item}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="result-section">
        <h3>Event Log</h3>
        <div className="event-log">
          {events.map((event, index) => (
            <div key={`${event.event}-${index}`}>
              <strong>{event.event}</strong>
              <span>{event.step_id || 'TASK'}</span>
              <p>{event.message}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="result-section">
        <h3>Google ADK Trace</h3>
        {adkTrace.length === 0 ? (
          <p className="muted-text">No ADK trace available.</p>
        ) : (
          <div className="event-log">
            {adkTrace.map((item, index) => (
              <div key={`${item.step_id}-${item.agent_name}-${index}`}>
                <strong>{item.agent_name}</strong>
                <span>{item.step_id}</span>
                <p>{item.tools?.length ? item.tools.join(', ') : 'state-only'} - {item.action}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="result-section">
        <div className="section-heading-row">
          <h3>Final Task Report</h3>
          {reportDownloadUrl && (
            <div className="flex gap-2">
              <a className="download-button" href={`${reportDownloadUrl}?format=pdf`} download>
                Download PDF Report
              </a>
              <a className="download-button secondary" href={`${reportDownloadUrl}?format=docx`} download>
                Download DOCX
              </a>
            </div>
          )}
          {importantPointsDownloadUrl && (
            <a className="download-button" href={importantPointsDownloadUrl} download>
              Download important points
            </a>
          )}
        </div>
        <pre className="report-box">{result.final_report || 'Final report will appear after final verification.'}</pre>
      </section>
    </article>
  )
}

export default TaskResults
