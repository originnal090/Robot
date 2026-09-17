using System.Collections.Generic;
using System;
using UnityEngine;

public class FireSpawner : MonoBehaviour
{
    [Serializable]
    public class SpawnTarget
    {
        public string id = "default";
        public Transform anchor;
    }

    [Header("Event Source")]
    public RobotSyncManager robotSyncManager;

    [Header("Fire Setup")]
    public GameObject firePrefab;
    public Transform defaultSpawnPoint;
    public float verticalOffset = 0f;
    public bool parentToAnchor = false;

    [Header("Target Mapping")]
    public List<SpawnTarget> targets = new List<SpawnTarget>();
    public bool onlyOneFirePerTarget = true;
    public float respawnCooldown = 0.5f;

    [Header("Debug")]
    public bool enableKeyboardDebug = false;
    public KeyCode debugKey = KeyCode.F;

    private readonly Dictionary<string, float> _lastSpawnTimes = new Dictionary<string, float>();
    private readonly Dictionary<string, GameObject> _spawnedFire = new Dictionary<string, GameObject>();

    private void OnEnable()
    {
        if (robotSyncManager != null)
        {
            robotSyncManager.onBallDetected.AddListener(HandleBallDetected);
        }
    }

    private void OnDisable()
    {
        if (robotSyncManager != null)
        {
            robotSyncManager.onBallDetected.RemoveListener(HandleBallDetected);
        }
    }

    private void Update()
    {
        if (enableKeyboardDebug && Input.GetKeyDown(debugKey))
        {
            HandleBallDetected(string.Empty);
        }
    }

    [ContextMenu("Spawn Fire At Default")]
    private void SpawnFireAtDefault()
    {
        HandleBallDetected(string.Empty);
    }

    public void HandleBallDetected(string targetId)
    {
        if (firePrefab == null)
        {
            Debug.LogWarning("[FireSpawner] firePrefab is not assigned.");
            return;
        }

        string resolvedId;
        Transform anchor = ResolveTarget(targetId, out resolvedId);
        if (anchor == null)
        {
            Debug.LogWarning("[FireSpawner] No spawn target is configured.");
            return;
        }

        GameObject existingFire;
        if (onlyOneFirePerTarget && _spawnedFire.TryGetValue(resolvedId, out existingFire))
        {
            if (existingFire != null)
            {
                Debug.Log("[FireSpawner] Fire already exists for target: " + resolvedId);
                return;
            }

            _spawnedFire.Remove(resolvedId);
        }

        float now = Time.time;
        float lastSpawnTime;
        if (_lastSpawnTimes.TryGetValue(resolvedId, out lastSpawnTime) && now - lastSpawnTime < respawnCooldown)
        {
            return;
        }

        Vector3 position = anchor.position + Vector3.up * verticalOffset;
        Transform parent = parentToAnchor ? anchor : null;
        GameObject fire = Instantiate(firePrefab, position, anchor.rotation, parent);
        _lastSpawnTimes[resolvedId] = now;

        if (onlyOneFirePerTarget)
        {
            _spawnedFire[resolvedId] = fire;
        }

        Debug.Log("[FireSpawner] Spawned fire for target: " + resolvedId);
    }

    public void ClearSpawnedFire()
    {
        foreach (var pair in _spawnedFire)
        {
            if (pair.Value != null)
            {
                Destroy(pair.Value);
            }
        }

        _spawnedFire.Clear();
        _lastSpawnTimes.Clear();
    }

    private Transform ResolveTarget(string targetId, out string resolvedId)
    {
        string normalizedId = string.IsNullOrWhiteSpace(targetId) ? string.Empty : targetId.Trim();

        if (!string.IsNullOrEmpty(normalizedId))
        {
            foreach (var target in targets)
            {
                if (target == null || target.anchor == null || string.IsNullOrWhiteSpace(target.id))
                {
                    continue;
                }

                if (string.Equals(target.id.Trim(), normalizedId, StringComparison.OrdinalIgnoreCase))
                {
                    resolvedId = target.id.Trim();
                    return target.anchor;
                }
            }
        }

        if (defaultSpawnPoint != null)
        {
            resolvedId = string.IsNullOrEmpty(normalizedId) ? "default" : normalizedId;
            return defaultSpawnPoint;
        }

        resolvedId = string.IsNullOrEmpty(normalizedId) ? gameObject.name : normalizedId;
        return transform;
    }
}
