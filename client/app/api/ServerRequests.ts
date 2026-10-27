import Server from "./Server";
import type { ApiResponse } from "./ApiResponse";
import type { CustomerSessionResponse } from '../schemas/CustomerSessionResponseSchema';
import type { DevUserListResponse } from '../schemas/DevUserListResponseSchema';
import type { FirebaseUserObject } from '../schemas/FirebaseUserObjectSchema';
import type { MyStoreResponse } from '../schemas/MyStoreResponseSchema';
import type { NoContentResponse } from '../schemas/NoContentResponseSchema';
import type { PhoneVerifiedResponse } from '../schemas/PhoneVerifiedResponseSchema';
import type { ProductListResponse } from '../schemas/ProductListResponseSchema';
import type { RoleListResponse } from '../schemas/RoleListResponseSchema';
import type { StoreResponse } from '../schemas/StoreResponseSchema';
import type { UserResponse } from '../schemas/UserResponseSchema';

    class ServerRequests extends Server {
    constructor(environment : string) {
        super(environment);
    }


    async loginWithGoogle(firebaseUserObject: FirebaseUserObject): Promise<ApiResponse<UserResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/loginWithGoogle`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"firebaseUserObject": firebaseUserObject}),
            cache: 'no-store'
        });
        const data: ApiResponse<UserResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        if (data.access_token) { localStorage.setItem("access_token", data.access_token); }
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async devGetUserList(): Promise<ApiResponse<DevUserListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/devGetUserList`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({}),
            cache: 'no-store'
        });
        const data: ApiResponse<DevUserListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async devLogin(userId: string): Promise<ApiResponse<UserResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/devLogin`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<UserResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        if (data.access_token) { localStorage.setItem("access_token", data.access_token); }
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getRoles(userId: string): Promise<ApiResponse<RoleListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getRoles`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<RoleListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getUserRoleTypes(userId: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getUserRoleTypes`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<NoContentResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getUserStore(userId: string): Promise<ApiResponse<MyStoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getUserStore`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<MyStoreResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async createUserStore(userId: string, name: string, description: string, contactNumber: string, address: string, currency: 'PHP' | 'USD' | 'EUR' | 'GBP' | 'JPY' | 'SGD' | 'AUD' | 'CAD' | 'HKD' | 'CNY' | 'KRW' | 'MYR' | 'THB' | 'IDR' | 'VND' | 'INR' | 'AED', openingHours: string | null): Promise<ApiResponse<StoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/createUserStore`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "name": name, "description": description, "contactNumber": contactNumber, "address": address, "currency": currency, "openingHours": openingHours}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async setUserStoreLocation(userId: string, latitude: number, longitude: number): Promise<ApiResponse<StoreResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/setUserStoreLocation`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "latitude": latitude, "longitude": longitude}),
            cache: 'no-store'
        });
        const data: ApiResponse<StoreResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async startGuestSession(idToken: string, storeSlug: string): Promise<ApiResponse<CustomerSessionResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/startGuestSession`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            
            },
            body: JSON.stringify({"idToken": idToken, "storeSlug": storeSlug}),
            cache: 'no-store'
        });
        const data: ApiResponse<CustomerSessionResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        if (data.access_token) { localStorage.setItem("access_token", data.access_token); }
        
        
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async verifyCustomerPhone(userId: string, idToken: string): Promise<ApiResponse<PhoneVerifiedResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/verifyCustomerPhone`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "idToken": idToken}),
            cache: 'no-store'
        });
        const data: ApiResponse<PhoneVerifiedResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async listStoreProducts(userId: string, storeId: string, search: string | null, limit: number): Promise<ApiResponse<ProductListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/listStoreProducts`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "search": search, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<ProductListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }
}

export interface RouteResponseModels {
    loginWithGoogle: UserResponse;
    devGetUserList: DevUserListResponse;
    devLogin: UserResponse;
    getRoles: RoleListResponse;
    getUserRoleTypes: NoContentResponse;
    getUserStore: MyStoreResponse;
    createUserStore: StoreResponse;
    setUserStoreLocation: StoreResponse;
    startGuestSession: CustomerSessionResponse;
    verifyCustomerPhone: PhoneVerifiedResponse;
    listStoreProducts: ProductListResponse;
}

export interface RouteRequestBodies {
    loginWithGoogle: { firebaseUserObject: FirebaseUserObject };
    devGetUserList: Record<string, never>;
    devLogin: { userId: string };
    getRoles: { userId: string };
    getUserRoleTypes: { userId: string };
    getUserStore: { userId: string };
    createUserStore: { userId: string; name: string; description: string; contactNumber: string; address: string; currency: 'PHP' | 'USD' | 'EUR' | 'GBP' | 'JPY' | 'SGD' | 'AUD' | 'CAD' | 'HKD' | 'CNY' | 'KRW' | 'MYR' | 'THB' | 'IDR' | 'VND' | 'INR' | 'AED'; openingHours: string | null };
    setUserStoreLocation: { userId: string; latitude: number; longitude: number };
    startGuestSession: { idToken: string; storeSlug: string };
    verifyCustomerPhone: { userId: string; idToken: string };
    listStoreProducts: { userId: string; storeId: string; search: string | null; limit: number };
}

export type PublicRoute =
    | 'devGetUserList';

export default ServerRequests;
