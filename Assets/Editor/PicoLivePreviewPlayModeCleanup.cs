using System;
using System.Collections.Generic;
using Unity.XR.PICO.LivePreview;
using UnityEditor;
using UnityEngine;
using UnityEngine.XR;
using UnityEngine.XR.Management;

[InitializeOnLoad]
internal static class PicoLivePreviewPlayModeCleanup
{
    static PicoLivePreviewPlayModeCleanup()
    {
        EditorApplication.playModeStateChanged += OnPlayModeStateChanged;
    }

    private static void OnPlayModeStateChanged(PlayModeStateChange state)
    {
        if (state == PlayModeStateChange.ExitingEditMode ||
            state == PlayModeStateChange.ExitingPlayMode ||
            state == PlayModeStateChange.EnteredEditMode)
        {
            CleanupLivePreviewState();
        }
    }

    [MenuItem("Tools/PICO/Cleanup Live Preview State")]
    private static void CleanupFromMenu()
    {
        CleanupLivePreviewState();
        Debug.Log("[PicoLivePreviewPlayModeCleanup] Live Preview state cleanup completed.");
    }

    private static void CleanupLivePreviewState()
    {
        ResetSrpState();
        StopStandaloneLoader();
        StopSubsystems();
    }

    private static void ResetSrpState()
    {
        try
        {
            PXR_PTApi.UPxr_PTSetSRPState(false);
        }
        catch (Exception exception)
        {
            Debug.LogWarning($"[PicoLivePreviewPlayModeCleanup] Failed to reset SRP state: {exception.Message}");
        }
    }

    private static void StopStandaloneLoader()
    {
        try
        {
            XRManagerSettings manager = XRGeneralSettings.Instance?.Manager;
            if (manager == null)
            {
                return;
            }

            if (manager.activeLoader != null)
            {
                manager.StopSubsystems();
                manager.DeinitializeLoader();
            }
        }
        catch (Exception exception)
        {
            Debug.LogWarning($"[PicoLivePreviewPlayModeCleanup] Failed to stop XR loader: {exception.Message}");
        }
    }

    private static void StopSubsystems()
    {
        StopSubsystemList<XRDisplaySubsystem>();
        StopSubsystemList<XRInputSubsystem>();
    }

    private static void StopSubsystemList<T>() where T : IntegratedSubsystem
    {
        try
        {
            List<T> subsystems = new List<T>();
            SubsystemManager.GetSubsystems(subsystems);

            foreach (T subsystem in subsystems)
            {
                if (subsystem != null && subsystem.running)
                {
                    subsystem.Stop();
                }
            }
        }
        catch (Exception exception)
        {
            Debug.LogWarning($"[PicoLivePreviewPlayModeCleanup] Failed to stop {typeof(T).Name}: {exception.Message}");
        }
    }
}
