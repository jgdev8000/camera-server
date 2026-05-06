import React, { useState, useEffect, useRef } from 'react';

/**
 * CameraStreamer Component
 *
 * Displays live video stream from the EPICS camera server
 * and provides snapshot functionality
 *
 * Usage:
 * <CameraStreamer serverUrl="http://localhost:8001" />
 */
const CameraStreamer = ({ serverUrl = 'http://localhost:8001' }) => {
  const [isConnected, setIsConnected] = useState(false);
  const [health, setHealth] = useState(null);
  const [info, setInfo] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(null);
  const [isTakingSnapshot, setIsTakingSnapshot] = useState(false);
  const [lastSnapshotTime, setLastSnapshotTime] = useState(null);
  const imageRef = useRef(null);

  // Check server health on mount
  useEffect(() => {
    const checkHealth = async () => {
      try {
        const response = await fetch(`${serverUrl}/health`);
        if (response.ok) {
          const data = await response.json();
          setHealth(data);
          setIsConnected(true);
          setError(null);
        } else {
          setError('Server returned error: ' + response.status);
          setIsConnected(false);
        }
      } catch (err) {
        setError('Failed to connect to camera server: ' + err.message);
        setIsConnected(false);
      } finally {
        setIsLoading(false);
      }
    };

    checkHealth();
    const interval = setInterval(checkHealth, 5000); // Check every 5 seconds

    return () => clearInterval(interval);
  }, [serverUrl]);

  // Get camera info on mount
  useEffect(() => {
    const fetchInfo = async () => {
      try {
        const response = await fetch(`${serverUrl}/info`);
        if (response.ok) {
          const data = await response.json();
          setInfo(data);
        }
      } catch (err) {
        console.error('Failed to fetch camera info:', err);
      }
    };

    fetchInfo();
  }, [serverUrl]);

  // Handle snapshot
  const handleSnapshot = async () => {
    setIsTakingSnapshot(true);
    try {
      const response = await fetch(`${serverUrl}/snapshot`);
      if (response.ok) {
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);

        // Download the snapshot
        const link = document.createElement('a');
        link.href = url;
        link.download = `snapshot-${Date.now()}.jpg`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);

        setLastSnapshotTime(new Date().toLocaleTimeString());
        setError(null);
      } else {
        setError('Failed to capture snapshot');
      }
    } catch (err) {
      setError('Snapshot error: ' + err.message);
    } finally {
      setIsTakingSnapshot(false);
    }
  };

  if (isLoading) {
    return (
      <div style={styles.container}>
        <div style={styles.loading}>Loading camera...</div>
      </div>
    );
  }

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <h1 style={styles.title}>EPICS Camera Stream</h1>
        <div style={styles.statusIndicator}>
          <span
            style={{
              ...styles.statusDot,
              backgroundColor: isConnected ? '#10b981' : '#ef4444'
            }}
          />
          <span style={styles.statusText}>
            {isConnected ? 'Connected' : 'Disconnected'}
          </span>
        </div>
      </div>

      {error && (
        <div style={styles.errorBanner}>
          <span>⚠️ {error}</span>
        </div>
      )}

      {isConnected && (
        <div style={styles.mainContent}>
          <div style={styles.streamContainer}>
            <img
              ref={imageRef}
              src={`${serverUrl}/stream`}
              alt="Camera Stream"
              style={styles.stream}
              onError={() => setError('Failed to load stream')}
              onLoad={() => setError(null)}
            />
            <div style={styles.streamOverlay}>
              <span>Live Stream</span>
            </div>
          </div>

          <div style={styles.controlsPanel}>
            <button
              onClick={handleSnapshot}
              disabled={isTakingSnapshot}
              style={{
                ...styles.button,
                ...styles.snapshotButton,
                opacity: isTakingSnapshot ? 0.6 : 1,
                cursor: isTakingSnapshot ? 'not-allowed' : 'pointer'
              }}
            >
              {isTakingSnapshot ? '📷 Capturing...' : '📷 Take Snapshot'}
            </button>

            {lastSnapshotTime && (
              <div style={styles.statusMessage}>
                ✓ Last snapshot: {lastSnapshotTime}
              </div>
            )}
          </div>

          {info && (
            <div style={styles.infoPanel}>
              <h3 style={styles.infoPanelTitle}>Camera Settings</h3>
              <div style={styles.infoPanelContent}>
                <div style={styles.infoRow}>
                  <span>Resolution:</span>
                  <span>
                    {info.resolution.width}x{info.resolution.height}
                  </span>
                </div>
                <div style={styles.infoRow}>
                  <span>Frame Rate:</span>
                  <span>{info.fps} FPS</span>
                </div>
                <div style={styles.infoRow}>
                  <span>Codec:</span>
                  <span>{info.codec.toUpperCase()}</span>
                </div>
                <div style={styles.infoRow}>
                  <span>Bitrate:</span>
                  <span>{info.bitrate}</span>
                </div>
              </div>
            </div>
          )}

          {health && (
            <div style={styles.healthPanel}>
              <h3 style={styles.healthPanelTitle}>Server Status</h3>
              <div style={styles.healthContent}>
                <div style={styles.healthRow}>
                  <span>Server:</span>
                  <span style={styles.healthGood}>{health.status}</span>
                </div>
                <div style={styles.healthRow}>
                  <span>Camera:</span>
                  <span
                    style={
                      health.camera === 'connected'
                        ? styles.healthGood
                        : styles.healthWarning
                    }
                  >
                    {health.camera}
                  </span>
                </div>
                <div style={styles.healthRow}>
                  <span>EPICS:</span>
                  <span
                    style={
                      health.epics_available
                        ? styles.healthGood
                        : styles.healthWarning
                    }
                  >
                    {health.epics_available ? 'Available' : 'Demo Mode'}
                  </span>
                </div>
              </div>
            </div>
          )}
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
  title: {
    margin: 0,
    fontSize: '28px',
    fontWeight: 'bold',
    color: '#1f2937',
  },
  statusIndicator: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '8px 12px',
    backgroundColor: 'white',
    borderRadius: '6px',
    border: '1px solid #e5e7eb',
  },
  statusDot: {
    width: '8px',
    height: '8px',
    borderRadius: '50%',
  },
  statusText: {
    fontSize: '14px',
    fontWeight: '500',
    color: '#4b5563',
  },
  loading: {
    textAlign: 'center',
    padding: '40px',
    fontSize: '18px',
    color: '#6b7280',
  },
  errorBanner: {
    backgroundColor: '#fee2e2',
    border: '1px solid #fecaca',
    color: '#991b1b',
    padding: '12px 16px',
    borderRadius: '6px',
    marginBottom: '16px',
    fontSize: '14px',
  },
  mainContent: {
    display: 'grid',
    gridTemplateColumns: '1fr 320px',
    gap: '20px',
  },
  streamContainer: {
    position: 'relative',
    backgroundColor: 'white',
    borderRadius: '8px',
    overflow: 'hidden',
    boxShadow: '0 1px 3px rgba(0,0,0,0.1)',
    aspectRatio: '16/12',
  },
  stream: {
    width: '100%',
    height: '100%',
    objectFit: 'contain',
    backgroundColor: '#000',
  },
  streamOverlay: {
    position: 'absolute',
    bottom: '12px',
    left: '12px',
    backgroundColor: 'rgba(0,0,0,0.6)',
    color: 'white',
    padding: '6px 12px',
    borderRadius: '4px',
    fontSize: '12px',
    fontWeight: '500',
  },
  controlsPanel: {
    display: 'flex',
    flexDirection: 'column',
    gap: '12px',
    gridColumn: '2',
  },
  button: {
    padding: '12px 16px',
    borderRadius: '6px',
    border: 'none',
    fontSize: '14px',
    fontWeight: '600',
    transition: 'all 0.2s',
    cursor: 'pointer',
  },
  snapshotButton: {
    backgroundColor: '#3b82f6',
    color: 'white',
  },
  statusMessage: {
    padding: '10px 12px',
    backgroundColor: '#ecfdf5',
    border: '1px solid #a7f3d0',
    color: '#065f46',
    borderRadius: '6px',
    fontSize: '12px',
    textAlign: 'center',
  },
  infoPanel: {
    gridColumn: '2',
    backgroundColor: 'white',
    borderRadius: '8px',
    padding: '16px',
    boxShadow: '0 1px 3px rgba(0,0,0,0.1)',
  },
  infoPanelTitle: {
    margin: '0 0 12px 0',
    fontSize: '14px',
    fontWeight: '600',
    color: '#1f2937',
  },
  infoPanelContent: {
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
  },
  infoRow: {
    display: 'flex',
    justifyContent: 'space-between',
    fontSize: '13px',
    color: '#4b5563',
  },
  healthPanel: {
    gridColumn: '2',
    backgroundColor: 'white',
    borderRadius: '8px',
    padding: '16px',
    boxShadow: '0 1px 3px rgba(0,0,0,0.1)',
  },
  healthPanelTitle: {
    margin: '0 0 12px 0',
    fontSize: '14px',
    fontWeight: '600',
    color: '#1f2937',
  },
  healthContent: {
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
  },
  healthRow: {
    display: 'flex',
    justifyContent: 'space-between',
    fontSize: '13px',
    alignItems: 'center',
  },
  healthGood: {
    color: '#10b981',
    fontWeight: '500',
  },
  healthWarning: {
    color: '#f59e0b',
    fontWeight: '500',
  },
};

export default CameraStreamer;
