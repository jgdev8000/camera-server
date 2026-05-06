import React, { useState, useEffect, useRef, useCallback } from 'react';

/**
 * CameraStreamer Component
 *
 * Multi-camera stream viewer with exposure / gain controls.
 *
 * Usage:
 *   <CameraStreamer serverUrl="http://localhost:8004" />
 */
const CameraStreamer = ({ serverUrl = 'http://localhost:8004' }) => {
  const [health, setHealth] = useState(null);
  const [cameras, setCameras] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [info, setInfo] = useState(null);
  const [control, setControl] = useState({ exposure: null, gain: null });
  const [exposureInput, setExposureInput] = useState('');
  const [gainInput, setGainInput] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);
  const [isApplying, setIsApplying] = useState(false);
  const [lastSnapshotTime, setLastSnapshotTime] = useState(null);
  const imageRef = useRef(null);

  const isConnected = health?.status === 'healthy';

  const fetchHealth = useCallback(async () => {
    try {
      const res = await fetch(`${serverUrl}/health`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setHealth(await res.json());
      setError(null);
    } catch (err) {
      setError(`Failed to reach camera server: ${err.message}`);
      setHealth(null);
    }
  }, [serverUrl]);

  const fetchCameras = useCallback(async () => {
    try {
      const res = await fetch(`${serverUrl}/cameras`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCameras(data.cameras);
      setSelectedId((current) => {
        if (current && data.cameras.some((c) => c.id === current)) return current;
        return data.default || data.cameras[0]?.id || null;
      });
    } catch (err) {
      console.error('Failed to fetch cameras:', err);
    }
  }, [serverUrl]);

  const fetchInfo = useCallback(async (id) => {
    try {
      const res = await fetch(`${serverUrl}/cameras/${id}`);
      if (res.ok) setInfo(await res.json());
    } catch (err) {
      console.error('Failed to fetch info:', err);
    }
  }, [serverUrl]);

  const fetchControl = useCallback(async (id) => {
    try {
      const res = await fetch(`${serverUrl}/cameras/${id}/control`);
      if (!res.ok) return;
      const data = await res.json();
      setControl(data);
      setExposureInput(data.exposure != null ? String(data.exposure) : '');
      setGainInput(data.gain != null ? String(data.gain) : '');
    } catch (err) {
      console.error('Failed to fetch control:', err);
    }
  }, [serverUrl]);

  // initial + periodic health/cameras
  useEffect(() => {
    const init = async () => {
      await Promise.all([fetchHealth(), fetchCameras()]);
      setIsLoading(false);
    };
    init();
    const t = setInterval(() => {
      fetchHealth();
      fetchCameras();
    }, 5000);
    return () => clearInterval(t);
  }, [fetchHealth, fetchCameras]);

  // refresh info + control on selection change, plus periodic control poll
  useEffect(() => {
    if (!selectedId) {
      setInfo(null);
      setControl({ exposure: null, gain: null });
      return;
    }
    fetchInfo(selectedId);
    fetchControl(selectedId);
    const t = setInterval(() => fetchControl(selectedId), 5000);
    return () => clearInterval(t);
  }, [selectedId, fetchInfo, fetchControl]);

  const handleSnapshot = async () => {
    if (!selectedId) return;
    try {
      const res = await fetch(`${serverUrl}/cameras/${selectedId}/snapshot`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${selectedId}-${Date.now()}.jpg`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      setLastSnapshotTime(new Date().toLocaleTimeString());
    } catch (err) {
      setError(`Snapshot error: ${err.message}`);
    }
  };

  const handleApplyControl = async () => {
    if (!selectedId) return;
    setIsApplying(true);
    try {
      const body = {};
      if (exposureInput !== '' && Number(exposureInput) !== control.exposure) {
        body.exposure = Number(exposureInput);
      }
      if (gainInput !== '' && Number(gainInput) !== control.gain) {
        body.gain = Number(gainInput);
      }
      if (Object.keys(body).length === 0) return;
      const res = await fetch(`${serverUrl}/cameras/${selectedId}/control`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(`HTTP ${res.status}: ${detail}`);
      }
      const data = await res.json();
      setControl(data.current);
      setError(null);
    } catch (err) {
      setError(`Control error: ${err.message}`);
    } finally {
      setIsApplying(false);
    }
  };

  if (isLoading) {
    return <div style={styles.container}><div style={styles.loading}>Loading cameras…</div></div>;
  }

  const selectedStatus = cameras.find((c) => c.id === selectedId)?.status;

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <h1 style={styles.title}>EPICS Camera Stream</h1>
        <div style={styles.statusIndicator}>
          <span style={{ ...styles.statusDot, backgroundColor: isConnected ? '#10b981' : '#ef4444' }} />
          <span style={styles.statusText}>{isConnected ? 'Server connected' : 'Disconnected'}</span>
        </div>
      </div>

      {error && <div style={styles.errorBanner}>⚠️ {error}</div>}

      <div style={styles.cameraSelector}>
        <label htmlFor="camera-select" style={styles.cameraSelectorLabel}>Camera:</label>
        <select
          id="camera-select"
          value={selectedId || ''}
          onChange={(e) => setSelectedId(e.target.value)}
          style={styles.select}
          disabled={cameras.length === 0}
        >
          {cameras.length === 0 && <option value="">No cameras configured</option>}
          {cameras.map((c) => (
            <option key={c.id} value={c.id}>
              {c.label} ({c.id}){c.is_default ? ' — default' : ''} — {c.status}
            </option>
          ))}
        </select>
      </div>

      {selectedId && (
        <div style={styles.mainContent}>
          <div style={styles.streamContainer}>
            <img
              ref={imageRef}
              key={`${selectedId}-stream`}
              src={`${serverUrl}/cameras/${selectedId}/stream`}
              alt={`${selectedId} stream`}
              style={styles.stream}
              onError={() => setError('Failed to load stream')}
              onLoad={() => setError(null)}
            />
            <div style={styles.streamOverlay}>
              <span>Live: {selectedId} ({selectedStatus})</span>
            </div>
          </div>

          <div style={styles.sidebar}>
            <div style={styles.panel}>
              <h3 style={styles.panelTitle}>Controls</h3>
              <label style={styles.controlLabel}>
                Exposure (s)
                <input
                  type="number"
                  step="0.001"
                  min="0"
                  value={exposureInput}
                  onChange={(e) => setExposureInput(e.target.value)}
                  style={styles.numberInput}
                />
              </label>
              <div style={styles.rbv}>
                RBV: {control.exposure != null ? control.exposure.toFixed(4) : '—'}
              </div>
              <label style={styles.controlLabel}>
                Gain
                <input
                  type="number"
                  step="0.1"
                  value={gainInput}
                  onChange={(e) => setGainInput(e.target.value)}
                  style={styles.numberInput}
                />
              </label>
              <div style={styles.rbv}>
                RBV: {control.gain != null ? control.gain.toFixed(2) : '—'}
              </div>
              <button
                onClick={handleApplyControl}
                disabled={isApplying}
                style={{
                  ...styles.button,
                  ...styles.applyButton,
                  opacity: isApplying ? 0.6 : 1,
                  cursor: isApplying ? 'not-allowed' : 'pointer',
                }}
              >
                {isApplying ? 'Applying…' : 'Apply'}
              </button>
              <button
                onClick={handleSnapshot}
                style={{ ...styles.button, ...styles.snapshotButton }}
              >
                📷 Snapshot
              </button>
              {lastSnapshotTime && (
                <div style={styles.statusMessage}>✓ Last snapshot: {lastSnapshotTime}</div>
              )}
            </div>

            {info && (
              <div style={styles.panel}>
                <h3 style={styles.panelTitle}>Camera Settings</h3>
                <div style={styles.infoRow}><span>Type:</span><span>{info.type}</span></div>
                <div style={styles.infoRow}><span>Prefix:</span><span>{info.prefix}</span></div>
                <div style={styles.infoRow}><span>Resolution:</span><span>{info.resolution.width}×{info.resolution.height}</span></div>
                <div style={styles.infoRow}><span>FPS:</span><span>{info.fps}</span></div>
              </div>
            )}

            {health && (
              <div style={styles.panel}>
                <h3 style={styles.panelTitle}>Server</h3>
                <div style={styles.infoRow}>
                  <span>Status:</span>
                  <span style={styles.healthGood}>{health.status}</span>
                </div>
                <div style={styles.infoRow}>
                  <span>EPICS:</span>
                  <span style={health.epics_available ? styles.healthGood : styles.healthWarning}>
                    {health.epics_available ? 'Available' : 'Demo mode'}
                  </span>
                </div>
                <div style={styles.infoRow}>
                  <span>Default:</span>
                  <span>{health.default || '—'}</span>
                </div>
                <div style={styles.infoRow}>
                  <span>Cameras:</span>
                  <span>{health.cameras.length}</span>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

const styles = {
  container: {
    maxWidth: '1200px',
    margin: '0 auto',
    padding: '20px',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    backgroundColor: '#f8f9fa',
    minHeight: '100vh',
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: '24px',
    paddingBottom: '16px',
    borderBottom: '2px solid #e5e7eb',
  },
  title: { margin: 0, fontSize: '28px', fontWeight: 'bold', color: '#1f2937' },
  statusIndicator: {
    display: 'flex', alignItems: 'center', gap: '8px',
    padding: '8px 12px', backgroundColor: 'white',
    borderRadius: '6px', border: '1px solid #e5e7eb',
  },
  statusDot: { width: '8px', height: '8px', borderRadius: '50%' },
  statusText: { fontSize: '14px', fontWeight: '500', color: '#4b5563' },
  loading: { textAlign: 'center', padding: '40px', fontSize: '18px', color: '#6b7280' },
  errorBanner: {
    backgroundColor: '#fee2e2', border: '1px solid #fecaca',
    color: '#991b1b', padding: '12px 16px', borderRadius: '6px',
    marginBottom: '16px', fontSize: '14px',
  },
  cameraSelector: {
    display: 'flex', alignItems: 'center', gap: '12px',
    padding: '12px 16px', backgroundColor: 'white',
    borderRadius: '6px', border: '1px solid #e5e7eb',
    marginBottom: '20px',
  },
  cameraSelectorLabel: { fontSize: '14px', fontWeight: '500', color: '#1f2937' },
  select: {
    flex: 1, padding: '8px 10px', fontSize: '14px',
    border: '1px solid #d1d5db', borderRadius: '4px',
    backgroundColor: 'white',
  },
  mainContent: {
    display: 'grid', gridTemplateColumns: '1fr 320px', gap: '20px',
  },
  streamContainer: {
    position: 'relative', backgroundColor: 'white',
    borderRadius: '8px', overflow: 'hidden',
    boxShadow: '0 1px 3px rgba(0,0,0,0.1)', aspectRatio: '16/12',
  },
  stream: {
    width: '100%', height: '100%', objectFit: 'contain', backgroundColor: '#000',
  },
  streamOverlay: {
    position: 'absolute', bottom: '12px', left: '12px',
    backgroundColor: 'rgba(0,0,0,0.6)', color: 'white',
    padding: '6px 12px', borderRadius: '4px',
    fontSize: '12px', fontWeight: '500',
  },
  sidebar: { display: 'flex', flexDirection: 'column', gap: '16px' },
  panel: {
    backgroundColor: 'white', borderRadius: '8px', padding: '16px',
    boxShadow: '0 1px 3px rgba(0,0,0,0.1)',
    display: 'flex', flexDirection: 'column', gap: '8px',
  },
  panelTitle: { margin: '0 0 4px 0', fontSize: '14px', fontWeight: '600', color: '#1f2937' },
  controlLabel: {
    display: 'flex', flexDirection: 'column', gap: '4px',
    fontSize: '13px', color: '#4b5563',
  },
  numberInput: {
    padding: '6px 8px', fontSize: '13px',
    border: '1px solid #d1d5db', borderRadius: '4px',
  },
  rbv: { fontSize: '12px', color: '#6b7280', marginTop: '-4px' },
  button: {
    padding: '10px 14px', borderRadius: '6px', border: 'none',
    fontSize: '14px', fontWeight: '600', cursor: 'pointer',
  },
  applyButton: { backgroundColor: '#10b981', color: 'white' },
  snapshotButton: { backgroundColor: '#3b82f6', color: 'white' },
  statusMessage: {
    padding: '8px 10px', backgroundColor: '#ecfdf5',
    border: '1px solid #a7f3d0', color: '#065f46',
    borderRadius: '6px', fontSize: '12px', textAlign: 'center',
  },
  infoRow: { display: 'flex', justifyContent: 'space-between', fontSize: '13px', color: '#4b5563' },
  healthGood: { color: '#10b981', fontWeight: '500' },
  healthWarning: { color: '#f59e0b', fontWeight: '500' },
};

export default CameraStreamer;
