import Server from "./Server";
import type { ApiResponse } from "./ApiResponse";
import type { BrandColorsAnswer } from '../schemas/BrandColorsAnswerSchema';
import type { ChatReplyResponse } from '../schemas/ChatReplyResponseSchema';
import type { ChatThinkingResponse } from '../schemas/ChatThinkingResponseSchema';
import type { ConversationListResponse } from '../schemas/ConversationListResponseSchema';
import type { ConversationTurnsResponse } from '../schemas/ConversationTurnsResponseSchema';
import type { CustomerSessionResponse } from '../schemas/CustomerSessionResponseSchema';
import type { DeliveryPinAnswer } from '../schemas/DeliveryPinAnswerSchema';
import type { DeliveryZonesAnswer } from '../schemas/DeliveryZonesAnswerSchema';
import type { DeliveryZonesResponse } from '../schemas/DeliveryZonesResponseSchema';
import type { FirebaseUserObject } from '../schemas/FirebaseUserObjectSchema';
import type { HandoverAnswer } from '../schemas/HandoverAnswerSchema';
import type { LocationAnswer } from '../schemas/LocationAnswerSchema';
import type { MyStoreScreenResponse } from '../schemas/MyStoreScreenResponseSchema';
import type { NewDeliveryZone } from '../schemas/NewDeliveryZoneSchema';
import type { NewProductsAnswer } from '../schemas/NewProductsAnswerSchema';
import type { NoContentResponse } from '../schemas/NoContentResponseSchema';
import type { OrderItemsAnswer } from '../schemas/OrderItemsAnswerSchema';
import type { PaymentAnswer } from '../schemas/PaymentAnswerSchema';
import type { PaymentReceiptResponse } from '../schemas/PaymentReceiptResponseSchema';
import type { PhoneVerifiedAnswer } from '../schemas/PhoneVerifiedAnswerSchema';
import type { PhotoRequestAnswer } from '../schemas/PhotoRequestAnswerSchema';
import type { ProductPhotoAnswer } from '../schemas/ProductPhotoAnswerSchema';
import type { ReturnDecisionAnswer } from '../schemas/ReturnDecisionAnswerSchema';
import type { RoleListResponse } from '../schemas/RoleListResponseSchema';
import type { SpokenThinkingResponse } from '../schemas/SpokenThinkingResponseSchema';
import type { StorePolicyResponse } from '../schemas/StorePolicyResponseSchema';
import type { UserResponse } from '../schemas/UserResponseSchema';
import type { WaitingOnYouResponse } from '../schemas/WaitingOnYouResponseSchema';

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

    async getUserStore(userId: string): Promise<ApiResponse<MyStoreScreenResponse>> {
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
        const data: ApiResponse<MyStoreScreenResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async setMyDeliveryZones(userId: string, zones: NewDeliveryZone[]): Promise<ApiResponse<DeliveryZonesResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/setMyDeliveryZones`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "zones": zones}),
            cache: 'no-store'
        });
        const data: ApiResponse<DeliveryZonesResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async resetMyStore(userId: string, confirmName: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/resetMyStore`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "confirmName": confirmName}),
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

    async verifyCustomerPhone(userId: string, idToken: string): Promise<ApiResponse<NoContentResponse>> {
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

    async getWaitingOnYou(userId: string): Promise<ApiResponse<WaitingOnYouResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getWaitingOnYou`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<WaitingOnYouResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async addPaymentReceipt(userId: string, storeId: string, orderId: string, receiptUrl: string): Promise<ApiResponse<NoContentResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/addPaymentReceipt`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "orderId": orderId, "receiptUrl": receiptUrl}),
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

    async getPaymentReceipt(userId: string, orderId: string): Promise<ApiResponse<PaymentReceiptResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getPaymentReceipt`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "orderId": orderId}),
            cache: 'no-store'
        });
        const data: ApiResponse<PaymentReceiptResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getMyStorePolicy(userId: string): Promise<ApiResponse<StorePolicyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getMyStorePolicy`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId}),
            cache: 'no-store'
        });
        const data: ApiResponse<StorePolicyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async sendAdminChatMessage(userId: string, message: string | null, conversationId: string | null, form: LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer | DeliveryZonesAnswer | ProductPhotoAnswer | BrandColorsAnswer | null, attachments: string[] | null, turnKey: string | null, speak: boolean | null): Promise<ApiResponse<ChatReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/sendAdminChatMessage`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "message": message, "conversationId": conversationId, "form": form, "attachments": attachments, "turnKey": turnKey, "speak": speak}),
            cache: 'no-store'
        });
        const data: ApiResponse<ChatReplyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async sendStoreChatMessage(userId: string, storeId: string, message: string | null, conversationId: string | null, form: LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer | DeliveryZonesAnswer | ProductPhotoAnswer | BrandColorsAnswer | null, attachments: string[] | null, turnKey: string | null, speak: boolean | null): Promise<ApiResponse<ChatReplyResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/sendStoreChatMessage`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "message": message, "conversationId": conversationId, "form": form, "attachments": attachments, "turnKey": turnKey, "speak": speak}),
            cache: 'no-store'
        });
        const data: ApiResponse<ChatReplyResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getChatThinking(userId: string, turnKey: string): Promise<ApiResponse<ChatThinkingResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getChatThinking`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "turnKey": turnKey}),
            cache: 'no-store'
        });
        const data: ApiResponse<ChatThinkingResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async listMyConversations(userId: string, storeId: string | null, before: string | null, limit: number): Promise<ApiResponse<ConversationListResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/listMyConversations`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "storeId": storeId, "before": before, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<ConversationListResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async getConversationTurns(userId: string, conversationId: string, beforeSeq: number | null, limit: number): Promise<ApiResponse<ConversationTurnsResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/getConversationTurns`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "conversationId": conversationId, "beforeSeq": beforeSeq, "limit": limit}),
            cache: 'no-store'
        });
        const data: ApiResponse<ConversationTurnsResponse> & { msg?: string } = await res.json();
        if (res.status >= 500) { return { message: data.message, status: 0, data: null }; }
        
        if (data.msg) { localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return { message: data.msg, status: 401, data: null }; }
        if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }
        return data;
        } catch (error) {
            console.error("Error:", error);
            return { message: "Failed to fetch data", status: 0, data: null };
        }
    }

    async speakChatThinking(userId: string, turnKey: string): Promise<ApiResponse<SpokenThinkingResponse>> {
        try {
        const res = await fetch(`${this.apiUrl}/speakChatThinking`, {
            method: 'POST',
            headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${localStorage.getItem("access_token")}`
            },
            body: JSON.stringify({"userId": userId, "turnKey": turnKey}),
            cache: 'no-store'
        });
        const data: ApiResponse<SpokenThinkingResponse> & { msg?: string } = await res.json();
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
    devLogin: UserResponse;
    getRoles: RoleListResponse;
    getUserRoleTypes: NoContentResponse;
    getUserStore: MyStoreScreenResponse;
    setMyDeliveryZones: DeliveryZonesResponse;
    resetMyStore: NoContentResponse;
    startGuestSession: CustomerSessionResponse;
    verifyCustomerPhone: NoContentResponse;
    getWaitingOnYou: WaitingOnYouResponse;
    addPaymentReceipt: NoContentResponse;
    getPaymentReceipt: PaymentReceiptResponse;
    getMyStorePolicy: StorePolicyResponse;
    sendAdminChatMessage: ChatReplyResponse;
    sendStoreChatMessage: ChatReplyResponse;
    getChatThinking: ChatThinkingResponse;
    listMyConversations: ConversationListResponse;
    getConversationTurns: ConversationTurnsResponse;
    speakChatThinking: SpokenThinkingResponse;
}

export interface RouteRequestBodies {
    loginWithGoogle: { firebaseUserObject: FirebaseUserObject };
    devLogin: { userId: string };
    getRoles: { userId: string };
    getUserRoleTypes: { userId: string };
    getUserStore: { userId: string };
    setMyDeliveryZones: { userId: string; zones: NewDeliveryZone[] };
    resetMyStore: { userId: string; confirmName: string };
    startGuestSession: { idToken: string; storeSlug: string };
    verifyCustomerPhone: { userId: string; idToken: string };
    getWaitingOnYou: { userId: string };
    addPaymentReceipt: { userId: string; storeId: string; orderId: string; receiptUrl: string };
    getPaymentReceipt: { userId: string; orderId: string };
    getMyStorePolicy: { userId: string };
    sendAdminChatMessage: { userId: string; message: string | null; conversationId: string | null; form: LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer | DeliveryZonesAnswer | ProductPhotoAnswer | BrandColorsAnswer | null; attachments: string[] | null; turnKey: string | null; speak: boolean | null };
    sendStoreChatMessage: { userId: string; storeId: string; message: string | null; conversationId: string | null; form: LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer | DeliveryZonesAnswer | ProductPhotoAnswer | BrandColorsAnswer | null; attachments: string[] | null; turnKey: string | null; speak: boolean | null };
    getChatThinking: { userId: string; turnKey: string };
    listMyConversations: { userId: string; storeId: string | null; before: string | null; limit: number };
    getConversationTurns: { userId: string; conversationId: string; beforeSeq: number | null; limit: number };
    speakChatThinking: { userId: string; turnKey: string };
}

export type PublicRoute = never;

export default ServerRequests;
