using UnityEngine;

/// <summary>
/// Converts a confirmed perception detection into the existing RED signal.
/// The existing LightControl remains responsible for selecting a configured
/// fire anchor and spawning the fire effect.
/// </summary>
public sealed class PerceptionFireBridge : MonoBehaviour
{
    [SerializeField] private RobotSyncManager robotSyncManager;
    private bool _redSignalSent;

    private void Awake()
    {
        if (robotSyncManager == null)
            robotSyncManager = FindObjectOfType<RobotSyncManager>();
    }

    public void HandleDetectionChanged(bool detected)
    {
        if (!detected || _redSignalSent)
            return;

        if (robotSyncManager == null)
        {
            Debug.LogWarning("[Perception Fire] RobotSyncManager is not assigned.");
            return;
        }

        _redSignalSent = true;
        robotSyncManager.onColorSignalReceived?.Invoke("RED");
        Debug.Log("[Perception Fire] Confirmed detection -> RED");
    }
}
